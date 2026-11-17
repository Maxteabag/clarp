//! The desk where an app asks for the use of tools to be explained.
//!
//! An app asks about up to eight things at once, and asks again until each is settled. A
//! thing is read, cut into its parts, and each part answered by the cheapest tier that can:
//! a template, then what was learned. A thing whose every part is answered so is answered
//! at once. Anything else waits for the model: it is queued, and whoever runs the desk
//! takes up to eight waiting things of one reader at a time ([`Desk::claim`]), asks the
//! model about the parts nobody answered, and says what it said ([`Desk::complete`]). What
//! the model says of a part is learned, so the next call of that shape is answered at
//! once. A model that fails is not asked about the same thing again for a while.
//!
//! A thing is wanted by whoever asked for it: a row that is in view asks again within five
//! seconds, and one that scrolled away is let go, by name or by silence. What nobody wants
//! any more is not asked of the model.
//!
//! The desk is a book: every call says what time it is, nothing here waits, reads a file
//! or asks anybody. What has to outlast the Host (a learned row, an hour of the ledger)
//! is handed out as changes to keep ([`Desk::changes`]).
//!
//! What an app is answered is the Python Host's (`server/lib/tool_explanations.py`,
//! `ToolExplanations.request` and its worker; `tool_explanation_queue.py`), held by the
//! scenes of `tests/fixtures/tool-explanation-service-vectors.json`: the statuses, the
//! sentences, what the model is asked, what is learned, and what the ledger counts.

use std::collections::{BTreeMap, HashMap};
use std::sync::LazyLock;

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use super::learned::{self, Row, SOURCES_MODEL, Table};
use super::shapes::{self, Part};
use super::{classify, lookup, read, text, version, written};
use crate::python;

/// The words a model is asked with: for each reader, how to say it; how a sentence with
/// places is asked for; and that what it is shown is to be read, not obeyed. The Python
/// Host's own words, written out by `tools/tool-explanation-service-vectors.py`.
static WORDS: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../explain-prompts.json")).unwrap_or_default()
});

/// How a model is told to write for a reader.
pub fn instructions(level: u8) -> String {
    let words = &WORDS["levels"][level.to_string()];
    words.as_str().unwrap_or_default().to_string()
}

/// What a model is given to read: how a sentence with places is asked for, that what
/// follows is to be read and not obeyed, and the requests.
pub fn asked(requests: &[Value]) -> String {
    let said = |words: &Value| words.as_str().unwrap_or_default().to_string();
    format!(
        "{}{}{}",
        said(&WORDS["with_places"]),
        said(&WORDS["untrusted"]),
        python::dumps_ascii(&json!({ "requests": requests })),
    )
}

/// What a model is asked, in words: how to write for this reader, and the requests.
pub fn prompt(level: u8, requests: &[Value]) -> String {
    format!("{}\n{}", instructions(level), asked(requests))
}

/// The shape a model's answer has to have: for each request, by its number, a sentence
/// and a sentence with places. `bounded`: with how long each may be, for a tool that can
/// hold a model to that.
pub fn schema(requests: &[Value], bounded: bool) -> Value {
    let numbers: Vec<&Value> = requests.iter().map(|request| &request["id"]).collect();
    let words = |most: usize| {
        if bounded {
            json!({ "type": "string", "maxLength": most })
        } else {
            json!({ "type": "string" })
        }
    };
    json!({
        "type": "object",
        "properties": { "explanations": { "type": "array", "items": {
            "type": "object",
            "properties": {
                "id": { "type": "string", "enum": numbers },
                "text": words(learned::TEXT_MOST),
                "template": words(learned::TEMPLATE_MOST),
            },
            "required": ["id", "text", "template"],
            "additionalProperties": false,
        } } },
        "required": ["explanations"],
        "additionalProperties": false,
    })
}

/// What a model answered as a table by number, from the list it is asked to answer with
/// (`{"explanations": [{"id", "text", "template"}]}`). `None`: it is no such list, or it
/// answers twice for one number.
pub fn tabled(answer: &Value) -> Option<Value> {
    let mut table = Map::new();
    for row in answer.get("explanations")?.as_array()? {
        let id = row.get("id")?.as_str()?;
        let template = row.get("template").cloned().unwrap_or_else(|| text(""));
        let said = json!({ "text": row.get("text")?, "template": template });
        if table.insert(id.to_string(), said).is_some() {
            return None;
        }
    }
    Some(Value::Object(table))
}

/// How many things an app asks about at once, and a model is asked about at once.
pub const AT_ONCE: usize = 8;
/// How many things a request may let go of, and how long a name may be.
const RELEASED_MOST: usize = 64;
const NAME_MOST: usize = 128;
/// How many things may wait for the model, and how many may want one of them.
const WAITING_MOST: usize = 64;
const WANTED_BY_MOST: usize = 256;
/// For how long a row in view wants its thing after it asked, and one that named no view.
const VIEW_MS: i64 = 5_000;
const KEPT_MS: i64 = 24 * 60 * 60 * 1000;
/// For how long a view that let go stays let go.
const LET_GO_MS: i64 = 120_000;
/// For how long the answer a model just gave is handed out as it was given, and for how
/// long a thing that is told again and again as not explained is counted once.
const DELIVERED_MS: i64 = 600_000;
const TOLD_MS: i64 = 60_000;
/// The kinds of answer the ledger counts, in the order an app is shown them.
pub const TIERS: [&str; 8] = [
    "template",
    "learned",
    "exact_cache",
    "jev",
    "llm",
    "failed",
    "miss",
    "disabled",
];
const HOUR_MS: i64 = 60 * 60 * 1000;
/// How long the ledger is kept.
const LEDGER_MS: i64 = 90 * 24 * HOUR_MS;

/// The janitor that explains, as its Host has it now.
#[derive(Clone, Debug, PartialEq)]
pub struct Janitor {
    /// The model it asks.
    pub model: String,
    /// What it is now, a name that changes whenever it is set otherwise. `None`: it is
    /// paused, and nothing is explained.
    pub identity: Option<String>,
    /// The reader it explains for, where its owner chose one for every device.
    pub level: Option<u8>,
    /// What it explains from ([`learned::SOURCES_ALL`] and its neighbours).
    pub sources: u8,
}

/// How long the desk waits: before a thing that was just asked about goes to the model
/// (others may be asked about in the same moment), and before a thing the model failed at
/// is tried again.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Pace {
    pub debounce_ms: i64,
    pub failure_ms: i64,
}

impl Default for Pace {
    fn default() -> Self {
        Self {
            debounce_ms: 180,
            failure_ms: 60_000,
        }
    }
}

/// What an app asks, as it sent it.
#[derive(Clone, Copy, Debug)]
pub struct Asked<'a> {
    pub level: &'a Value,
    pub items: &'a Value,
    pub release: Option<&'a Value>,
    pub target: Option<&'a Value>,
    /// Whether a developer's view of where each answer came from is wanted.
    pub provenance: bool,
}

/// What a model is asked: the parts nobody answered, for one reader.
#[derive(Clone, Debug, PartialEq)]
pub struct Batch {
    pub level: u8,
    pub model: String,
    /// The janitor the things were asked of, as it was then ([`Janitor::identity`]).
    pub identity: String,
    /// Each a number as its `id`, what is shown of the part, and the values of its slots.
    pub requests: Vec<Value>,
}

/// What became of a question to the model.
#[derive(Clone, Debug, PartialEq)]
pub enum Said {
    /// What it answered: a sentence, or a sentence and a sentence with places, by number.
    Answered(Value),
    /// It did not answer, and why, in a word an app is told: `timeout`,
    /// `codex_unavailable`, `invalid_response`, `translator_failed`.
    Failed(&'static str),
}

/// What has to be kept beyond the Host's own life.
#[derive(Clone, Debug, PartialEq)]
pub enum Change {
    /// A row of what was learned, as it is now.
    Learned(Row),
    /// A row that is no more.
    Forgotten { signature: String, level: u8 },
    /// An hour of the ledger, as it is now: how many answers of each tier, and how long
    /// they took together.
    Counted {
        hour: i64,
        counts: [u64; 8],
        latency_ms: u64,
    },
}

/// A part of a thing on its way through the tiers.
#[derive(Clone, Debug)]
struct Unit {
    part: Part,
    route: Value,
    answer: Option<Value>,
}

/// A thing that waits for the model.
#[derive(Clone, Debug)]
struct Job {
    key: String,
    level: u8,
    identity: String,
    model: String,
    sources: u8,
    signature: String,
    units: Vec<Unit>,
    created_at: i64,
    available_at: i64,
    /// Who wants it, each until when.
    wanted: BTreeMap<String, i64>,
    running: bool,
}

/// What is known of a thing that was asked of the model.
#[derive(Clone, Debug)]
enum Kept {
    Ready { answer: Value, until: i64 },
    Failed { reason: String, until: i64 },
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
struct Hour {
    counts: [u64; 8],
    latency_ms: u64,
}

/// The desk.
#[derive(Debug, Default)]
pub struct Desk {
    /// The Host's own key, under which shapes are signed.
    key: String,
    pace: Pace,
    table: Table,
    jobs: Vec<Job>,
    kept: HashMap<String, Kept>,
    let_go: HashMap<String, i64>,
    quiet: HashMap<String, i64>,
    ledger: BTreeMap<i64, Hour>,
    /// What the model is being asked now: which part of which thing each number is, and
    /// why it came to the model.
    asking: Vec<(String, usize, String)>,
    changes: Vec<Change>,
}

fn named(value: &Value) -> Option<&str> {
    value
        .as_str()
        .filter(|name| (1..=NAME_MOST).contains(&name.chars().count()))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

impl Desk {
    /// A desk that signs with the Host's key, with what was learned and counted before.
    pub fn new(
        key: &str,
        pace: Pace,
        learned: impl IntoIterator<Item = Row>,
        counted: impl IntoIterator<Item = (i64, [u64; 8], u64)>,
    ) -> Self {
        let hours = counted
            .into_iter()
            .map(|(hour, counts, latency_ms)| (hour, Hour { counts, latency_ms }));
        Self {
            key: key.to_string(),
            pace,
            table: Table::of(learned),
            ledger: hours.collect(),
            ..Self::default()
        }
    }

    /// What was learned.
    pub fn table(&self) -> &Table {
        &self.table
    }

    /// What changed since this was last asked, to be kept.
    pub fn changes(&mut self) -> Vec<Change> {
        std::mem::take(&mut self.changes)
    }

    /// Whether anything waits for the model or is being asked of it.
    pub fn busy(&self) -> bool {
        !self.jobs.is_empty()
    }

    /// The janitors of whom something waits to be asked, each once, as they were when it
    /// was asked of them.
    pub fn asked_of(&self) -> Vec<String> {
        let mut of: Vec<String> = Vec::new();
        for job in self.jobs.iter().filter(|job| !job.running) {
            if !of.contains(&job.identity) {
                of.push(job.identity.clone());
            }
        }
        of
    }

    // ---- the ledger ----------------------------------------------------------------------

    fn record(&mut self, tier: &str, now: i64, latency_ms: i64) {
        let Some(place) = TIERS.iter().position(|known| *known == tier) else {
            return;
        };
        let hour = now.div_euclid(HOUR_MS) * HOUR_MS;
        let counted = self.ledger.entry(hour).or_default();
        counted.counts[place] += 1;
        counted.latency_ms += u64::try_from(latency_ms).unwrap_or(0);
        let (counts, latency_ms) = (counted.counts, counted.latency_ms);
        self.changes.push(Change::Counted {
            hour,
            counts,
            latency_ms,
        });
    }

    /// Counts an answer of a tier at a moment: for a ledger that is read back.
    pub fn count(&mut self, tier: &str, at_ms: i64, latency_ms: i64) {
        self.record(tier, at_ms, latency_ms);
    }

    /// Whether a thing is kept quiet now. One that is asked for the last time is quiet no
    /// longer.
    fn quieted(&mut self, key: &str, now: i64, last: bool) -> bool {
        let Some(until) = self.quiet.get(key).copied() else {
            return false;
        };
        if until <= now || last {
            self.quiet.remove(key);
        }
        until > now
    }

    fn hit(&mut self, answer: &Value, now: i64) {
        if answer["tier"] != "learned" {
            return;
        }
        let under = answer["hit"].as_str().unwrap_or_default();
        let level = answer["hit_level"]
            .as_u64()
            .and_then(|level| u8::try_from(level).ok());
        if let Some(row) = self.table.hit(under, level.unwrap_or(0), now) {
            self.changes.push(Change::Learned(row.clone()));
        }
        if let Some([old, new, program]) = answer["move"].as_array().map(Vec::as_slice) {
            let name = |value: &Value| value.as_str().unwrap_or_default().to_string();
            let (gone, there) = self.table.rename(&name(old), &name(new), &name(program));
            let forgotten = gone.into_iter().map(|row| Change::Forgotten {
                signature: row.signature,
                level: row.level,
            });
            self.changes.extend(forgotten);
            self.changes.extend(there.into_iter().map(Change::Learned));
        }
    }

    // ---- an app asks ---------------------------------------------------------------------

    /// Answers a request. `janitor`: the one that explains, where the Host has one.
    /// `scripts`: what was read of the scripts a part names, where its agent's directory is
    /// known. `Err`: the request is not one, in the words an app is told.
    pub fn request(
        &mut self,
        asked: &Asked<'_>,
        janitor: Option<&Janitor>,
        scripts: &dyn Fn(&Map<String, Value>) -> Vec<Value>,
        now: i64,
    ) -> Result<Value, &'static str> {
        let whole = asked.level.as_i64().filter(|level| (0..=4).contains(level));
        let requested = whole.and_then(|level| u8::try_from(level).ok());
        let requested = requested.ok_or("detail_level must be an integer from 0 to 4")?;
        let items = asked
            .items
            .as_array()
            .filter(|items| items.len() <= AT_ONCE);
        let items = items.ok_or("items must contain at most 8 activities")?;
        let released: Vec<&str> = match asked.release {
            None | Some(Value::Null) => Vec::new(),
            Some(Value::Array(views))
                if views.len() <= RELEASED_MOST
                    && views.iter().all(|view| named(view).is_some()) =>
            {
                views.iter().filter_map(Value::as_str).collect()
            }
            Some(_) => return Err("invalid released demand IDs"),
        };
        if asked
            .target
            .is_some_and(|target| !target.is_null() && named(target).is_none())
        {
            return Err("invalid target agent ID");
        }
        let model = janitor.map_or("", |janitor| janitor.model.as_str());
        let level = janitor.map_or(0, |janitor| janitor.level.unwrap_or(requested));
        let running = janitor.and_then(|janitor| janitor.identity.as_deref());
        let sources = janitor.map_or(0, |janitor| janitor.sources);
        let explains = level != 0 && running.is_some();

        // Each thing: read, cut into its parts, and each part answered where a cheap tier can.
        struct Entry<'a> {
            id: &'a str,
            view: Option<&'a str>,
            key: String,
            signature: String,
            units: Vec<Unit>,
            at_once: bool,
        }
        let mut entries: Vec<Entry<'_>> = Vec::with_capacity(items.len());
        for item in items {
            let id = item.get("id").and_then(named);
            let id = id.filter(|id| !entries.iter().any(|entry| entry.id == *id));
            let id = id.filter(|_| item.is_object());
            let id = id.ok_or("items need unique short string IDs")?;
            let view = match item.get("demand_id") {
                None | Some(Value::Null) => None,
                Some(view) => Some(named(view).ok_or("invalid demand ID")?),
            };
            let activity = item.get("activity").and_then(Value::as_object);
            let activity = read::normalize(activity.ok_or("activity must be an object")?);
            let (mut signature, mut units) = (String::new(), Vec::new());
            if explains {
                let route = classify(&activity, &self.key);
                signature = route["signature"].as_str().unwrap_or_default().to_string();
                let mut parts = shapes::split(&activity, &self.key);
                if let [only] = parts.as_mut_slice()
                    && only.exact
                {
                    // A part known by its whole text is never said of another call, so it may
                    // show whoever explains it everything an app sent of it.
                    only.activity.clone_from(&activity);
                }
                for mut part in parts {
                    let shown = part.template_activity();
                    let (answer, route) = if sources == SOURCES_MODEL {
                        (None, classify(&shown, &self.key))
                    } else {
                        let (said, route) = lookup(&shown, level, &self.key);
                        (said.map(|said| learned::scripted(&said, &route)), route)
                    };
                    if answer.is_none() {
                        let evidence = scripts(&part.activity);
                        part = shapes::with_scripts(part, &evidence);
                    }
                    let answer = answer.or_else(|| learned::learned(&part, &self.table, level));
                    units.push(Unit {
                        part,
                        route,
                        answer,
                    });
                }
            }
            let signatures: Vec<&str> = units
                .iter()
                .map(|unit| unit.part.signature.as_str())
                .collect();
            let named_by = json!([running, level, activity, signatures]);
            let key = hex(&Sha256::digest(written(&named_by).as_bytes()));
            // What a model just answered is handed out once as it was given, not as it reads
            // from the table.
            let delivering = self.quieted(&key, now, false);
            let answered = !units.is_empty() && units.iter().all(|unit| unit.answer.is_some());
            entries.push(Entry {
                id,
                view,
                key,
                signature,
                units,
                at_once: answered && !delivering,
            });
        }

        // What the model is needed for: answered from what it said before, or queued.
        let let_go_now = |view: &str| released.contains(&view);
        let mut answers: Vec<Value> = Vec::with_capacity(entries.len());
        let mut waiting = None::<usize>;
        let mut added = 0;
        let mut wants: Vec<usize> = Vec::new();
        for (place, entry) in entries.iter().enumerate() {
            let fenced = |view: &str| self.let_go.get(view).is_some_and(|until| *until > now);
            let gone = entry
                .view
                .is_some_and(|view| let_go_now(view) || fenced(view));
            if entry.at_once {
                let answers_of: Vec<Value> = entry
                    .units
                    .iter()
                    .filter_map(|unit| unit.answer.clone())
                    .collect();
                answers.push(if gone {
                    json!({ "status": "cancelled" })
                } else {
                    learned::assemble(&answers_of)
                });
                continue;
            }
            if gone {
                answers.push(json!({ "status": "cancelled" }));
                continue;
            }
            if !explains {
                answers.push(json!({ "status": "disabled" }));
                continue;
            }
            match self.kept.get(&entry.key) {
                Some(Kept::Ready { answer, until }) if *until > now => {
                    answers.push(json!({
                        "status": "ready", "text": answer["text"], "source": answer["source"],
                        "cached": true, "provenance": answer["provenance"],
                    }));
                    continue;
                }
                Some(Kept::Failed { reason, until }) if *until > now => {
                    answers.push(json!({ "status": "failed", "reason": reason }));
                    continue;
                }
                _ => {}
            }
            let job = self.jobs.iter().find(|job| job.key == entry.key);
            if job.is_none() {
                let queued = *waiting
                    .get_or_insert_with(|| self.jobs.iter().filter(|job| !job.running).count());
                if queued + added >= WAITING_MOST {
                    answers.push(json!({ "status": "busy", "reason": "queue_full" }));
                    continue;
                }
            }
            let owner = entry.view.unwrap_or("legacy");
            let wanted_by = job.map_or(0, |job| {
                job.wanted.values().filter(|until| **until > now).count()
            });
            let wants_already =
                job.is_some_and(|job| job.wanted.get(owner).is_some_and(|until| *until > now));
            if !wants_already && wanted_by >= WANTED_BY_MOST {
                answers.push(json!({ "status": "busy", "reason": "too_many_views" }));
                continue;
            }
            added += usize::from(job.is_none());
            wants.push(place);
            answers.push(json!({ "status": "pending" }));
        }

        // Who let go, lets go: what nobody wants any more does not go to the model.
        for view in &released {
            self.let_go.insert((*view).to_string(), now + LET_GO_MS);
            for job in &mut self.jobs {
                job.wanted.remove(*view);
            }
        }
        if !released.is_empty() {
            self.jobs
                .retain(|job| job.running || !job.wanted.is_empty());
        }
        for place in wants {
            let entry = &entries[place];
            self.kept.remove(&entry.key);
            let until = now
                + if entry.view.is_some() {
                    VIEW_MS
                } else {
                    KEPT_MS
                };
            let owner = entry.view.unwrap_or("legacy").to_string();
            if let Some(job) = self.jobs.iter_mut().find(|job| job.key == entry.key) {
                job.wanted.insert(owner, until);
                continue;
            }
            self.jobs.push(Job {
                key: entry.key.clone(),
                level,
                identity: running.unwrap_or_default().to_string(),
                model: model.to_string(),
                sources,
                signature: entry.signature.clone(),
                units: entry.units.clone(),
                created_at: now,
                available_at: now + self.pace.debounce_ms,
                wanted: BTreeMap::from([(owner, until)]),
                running: false,
            });
        }

        // What was answered from the table counts for its rows, and every answer for the
        // ledger: once for what a model just delivered, once a minute for what is told again
        // and again as not explained.
        for (entry, answer) in entries.iter().zip(&answers) {
            let status = answer["status"].as_str().unwrap_or_default();
            if entry.at_once && status == "ready" {
                for unit in &entry.units {
                    if let Some(answer) = &unit.answer {
                        self.hit(answer, now);
                    }
                }
            }
            match status {
                "ready" => {
                    if !self.quieted(&entry.key, now, true) {
                        let tier = answer["tier"].as_str().unwrap_or("exact_cache").to_string();
                        self.record(&tier, now, 0);
                    }
                }
                "busy" | "disabled" if !self.quieted(&entry.key, now, false) => {
                    self.quiet.insert(entry.key.clone(), now + TOLD_MS);
                    self.record(if status == "busy" { "miss" } else { "disabled" }, now, 0);
                }
                _ => {}
            }
        }

        let told: Vec<Value> = entries
            .iter()
            .zip(answers)
            .map(|(entry, answer)| {
                let mut told = Map::new();
                told.insert("id".to_string(), text(entry.id));
                for (key, value) in answer.as_object().into_iter().flatten() {
                    let developers = key == "provenance" && !asked.provenance;
                    if key != "tier" && !developers {
                        told.insert(key.clone(), value.clone());
                    }
                }
                Value::Object(told)
            })
            .collect();
        Ok(json!({ "model": model, "detail_level": level, "items": told }))
    }

    // ---- the model is asked --------------------------------------------------------------

    /// Lets go of what nobody wants any more and of what was kept long enough.
    fn tidy(&mut self, now: i64) {
        self.kept.retain(|_, kept| match kept {
            Kept::Ready { until, .. } | Kept::Failed { until, .. } => *until > now,
        });
        self.let_go.retain(|_, until| *until > now);
        self.quiet.retain(|_, until| *until > now);
        for job in &mut self.jobs {
            job.wanted.retain(|_, until| *until > now);
        }
        self.jobs
            .retain(|job| job.running || !job.wanted.is_empty());
        let oldest = now - LEDGER_MS;
        self.ledger.retain(|hour, _| *hour >= oldest);
    }

    /// What the model is to be asked now, if anything: the parts nobody answered of up to
    /// eight things that wait, the longest first, all for one reader and asked of one
    /// janitor. `None`: nothing waits that is due, or the model is being asked already.
    /// `current`: whether a janitor is still what it was when the things were asked of it;
    /// what one that is paused or set otherwise since would have explained is dropped
    /// unasked.
    pub fn claim(&mut self, current: &dyn Fn(&str) -> bool, now: i64) -> Option<Batch> {
        loop {
            self.tidy(now);
            if self.jobs.iter().any(|job| job.running) {
                return None;
            }
            let due = |job: &&Job| job.available_at <= now;
            let first = self
                .jobs
                .iter()
                .filter(due)
                .min_by_key(|job| job.created_at)?;
            let (level, identity, model) =
                (first.level, first.identity.clone(), first.model.clone());
            let sources = first.sources;
            let mut taken: Vec<usize> = (0..self.jobs.len())
                .filter(|place| {
                    let job = &self.jobs[*place];
                    job.available_at <= now && job.level == level && job.identity == identity
                })
                .collect();
            taken.sort_by_key(|place| self.jobs[*place].created_at);
            taken.truncate(AT_ONCE);
            if !current(&identity) {
                // The janitor is paused or set otherwise since: nothing of this is said.
                let keys: Vec<String> = taken
                    .iter()
                    .map(|place| self.jobs[*place].key.clone())
                    .collect();
                self.jobs.retain(|job| !keys.contains(&job.key));
                for _ in keys {
                    self.record("disabled", now, 0);
                }
                continue;
            }
            // Another thing may have taught the table meanwhile.
            self.asking.clear();
            let mut requests = Vec::new();
            for place in &taken {
                let job = &mut self.jobs[*place];
                job.running = true;
                for (number, unit) in job.units.iter_mut().enumerate() {
                    if unit.answer.is_none() {
                        unit.answer = learned::learned(&unit.part, &self.table, level);
                    }
                    if unit.answer.is_some() {
                        continue;
                    }
                    let mut request = Map::new();
                    request.insert("id".to_string(), text(&(requests.len() + 1).to_string()));
                    request.insert(
                        "activity".to_string(),
                        Value::Object(unit.part.activity.clone()),
                    );
                    if !unit.part.slots.is_empty() {
                        let slots = shapes::slot_values(&unit.part.slots);
                        request.insert("slots".to_string(), Value::Object(slots));
                    }
                    requests.push(Value::Object(request));
                    let reason = learned::fallback_reason(&unit.route, level, sources);
                    self.asking.push((job.key.clone(), number, reason));
                }
            }
            if requests.is_empty() {
                // Everything was answered without the model after all.
                self.publish(None, now);
                continue;
            }
            return Some(Batch {
                level,
                model,
                identity,
                requests,
            });
        }
    }

    /// Puts back what [`Desk::claim`] handed out, unasked: the janitor is at another
    /// request. It is due again after `delay_ms`.
    pub fn abandon(&mut self, now: i64, delay_ms: i64) {
        self.asking.clear();
        for job in self.jobs.iter_mut().filter(|job| job.running) {
            job.running = false;
            job.available_at = job.available_at.max(now + delay_ms);
        }
    }

    /// Says what the model answered to what [`Desk::claim`] handed out.
    pub fn complete(&mut self, said: &Said, now: i64) {
        let failure = match said {
            Said::Failed(reason) => Some(*reason),
            Said::Answered(answer) => match learned::answers(answer, self.asking.len()) {
                None => Some("translator_failed"),
                Some(read) => {
                    let asking = std::mem::take(&mut self.asking);
                    for ((key, number, reason), (said, template)) in asking.iter().zip(&read) {
                        let Some(job) = self.jobs.iter_mut().find(|job| job.key == *key) else {
                            continue;
                        };
                        let level = job.level;
                        let Some(unit) = job.units.get_mut(*number) else {
                            continue;
                        };
                        let (answer, row, _) = learned::explained(
                            &unit.part,
                            &unit.route,
                            said,
                            template,
                            level,
                            reason,
                            now,
                        );
                        unit.answer = Some(answer);
                        self.table.store(row.clone());
                        self.changes.push(Change::Learned(row));
                    }
                    None
                }
            },
        };
        self.publish(failure, now);
    }

    /// Settles the things the model was asked about: each is answered, or failed.
    fn publish(&mut self, failure: Option<&str>, now: i64) {
        self.asking.clear();
        let (running, waiting): (Vec<Job>, Vec<Job>) = std::mem::take(&mut self.jobs)
            .into_iter()
            .partition(|job| job.running);
        self.jobs = waiting;
        for job in running {
            let answers: Vec<Value> = job
                .units
                .iter()
                .filter_map(|unit| unit.answer.clone())
                .collect();
            let latency = (now - job.created_at).max(0);
            if answers.len() < job.units.len() {
                let reason = failure.unwrap_or("translator_failed").to_string();
                let until = now + self.pace.failure_ms;
                self.kept.insert(job.key, Kept::Failed { reason, until });
                self.record("failed", now, latency);
                continue;
            }
            for answer in &answers {
                self.hit(answer, now);
            }
            let mut whole = learned::assemble(&answers);
            let tier = whole["tier"].as_str().unwrap_or_default().to_string();
            whole["signature"] = text(&job.signature);
            let until = now + KEPT_MS;
            self.kept.insert(
                job.key.clone(),
                Kept::Ready {
                    answer: whole,
                    until,
                },
            );
            self.record(&tier, now, latency);
            // Whoever picks this up is not counted again.
            self.quiet.insert(job.key, now + DELIVERED_MS);
        }
    }

    /// Takes away what was learned under a library or a prompt that is no more, a month
    /// after it last answered, and of what holds for one text alone all but the hundred
    /// thousand most recently used. For now and then, not for every call.
    pub fn prune(&mut self, now: i64) {
        let gone = self.table.prune(now, 30 * 24 * HOUR_MS, 100_000);
        let forgotten = gone.into_iter().map(|row| Change::Forgotten {
            signature: row.signature,
            level: row.level,
        });
        self.changes.extend(forgotten);
    }

    // ---- how it went ---------------------------------------------------------------------

    /// How the explaining went: how many answers of each tier in each hour or day of a
    /// window, and what was learned. `Err`: no such window, or no such size of a bucket.
    pub fn stats(&self, window: &str, bucket: &str, now: i64) -> Result<Value, String> {
        let windows = [("24h", 24), ("7d", 7 * 24), ("30d", 30 * 24)];
        let buckets = [("hour", 1), ("day", 24)];
        let names = |known: &[(&str, i64)]| {
            let names: Vec<&str> = known.iter().map(|(name, _)| *name).collect();
            names.join(", ")
        };
        let Some((_, hours)) = windows.iter().find(|(name, _)| *name == window) else {
            return Err(format!("window must be one of {}", names(&windows)));
        };
        let Some((_, each)) = buckets.iter().find(|(name, _)| *name == bucket) else {
            return Err(format!("bucket must be one of {}", names(&buckets)));
        };
        let size = each * HOUR_MS;
        let first = (now - hours * HOUR_MS).div_euclid(size) * size + size;
        let rate = |counts: &[u64; 8]| {
            let lookups: u64 = counts[..7].iter().sum();
            let hits: u64 = counts[..3].iter().sum();
            let share = (lookups > 0).then(|| {
                let share = format!("{:.4}", hits as f64 / lookups as f64);
                share.parse::<f64>().unwrap_or(0.0)
            });
            (lookups, hits, share)
        };
        let counted = |counts: &[u64; 8]| {
            let by_tier: Map<String, Value> = TIERS
                .iter()
                .zip(counts)
                .map(|(tier, count)| ((*tier).to_string(), json!(count)))
                .collect();
            Value::Object(by_tier)
        };
        let mut total = [0u64; 8];
        let mut told = Vec::new();
        let mut start = first;
        while start <= now.div_euclid(size) * size {
            let mut of = Hour::default();
            for (_, hour) in self.ledger.range(start..start + size) {
                for (sum, count) in of.counts.iter_mut().zip(hour.counts) {
                    *sum += count;
                }
                of.latency_ms += hour.latency_ms;
            }
            for (sum, count) in total.iter_mut().zip(of.counts) {
                *sum += count;
            }
            let (lookups, hits, share) = rate(&of.counts);
            let answered: u64 = of.counts.iter().sum();
            let mean = (answered > 0)
                .then(|| (of.latency_ms as f64 / answered as f64).round_ties_even() as u64);
            told.push(json!({
                "start": start, "counts": counted(&of.counts), "lookups": lookups, "hits": hits,
                "hit_rate": share, "mean_latency_ms": mean,
            }));
            start += size;
        }
        let current = |row: &&Row| {
            row.prompt_version == learned::PROMPT_VERSION && row.templates_version == version()
        };
        let rows: Vec<&Row> = self.table.rows().filter(current).collect();
        let with_places = rows.iter().filter(|row| row.parameterised).count();
        let mut by: BTreeMap<&str, u64> = BTreeMap::new();
        for row in &rows {
            *by.entry(row.producer.as_str()).or_default() += 1;
        }
        let answered_again: u64 = rows.iter().map(|row| row.hits).sum();
        let (lookups, hits, share) = rate(&total);
        Ok(json!({
            "window": window, "bucket": bucket, "now": now, "tiers": TIERS, "buckets": told,
            "totals": {
                "counts": counted(&total), "lookups": lookups, "hits": hits, "hit_rate": share,
                "learned_entries": rows.len(), "learned_parameterised": with_places,
                "learned_exact_only": rows.len() - with_places, "learned_by_producer": by,
                "learned_hits": answered_again, "jev_picks": total[3], "llm_calls": total[4],
            },
        }))
    }
}
