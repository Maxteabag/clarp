//! What was said of a call before, and is said again without asking.
//!
//! A model is asked what a call does once. What it says is kept in a table, under the
//! call's shape where its sentence holds for other values (a sentence with places for the
//! call's values), and under the hash of the call's whole text where it holds for that
//! text alone. The next call of the same shape is answered from the table with its own
//! values in the places. A row is for one reader (the level of detail it was written for)
//! and holds while the library of templates and the prompt are the ones it was made
//! under. Nothing a tool was given is kept: a signature is a keyed hash, and a sentence
//! has places where values stood, except a sentence kept for one text alone, which is what
//! an app was shown.
//!
//! Here also: how the answers to a call's parts are put together into one, and what a
//! developer is told of where an answer came from.
//!
//! The rules are the Python Host's (`server/lib/tool_explanations.py`: `_learned`,
//! `_assemble`, `_provenance`, `_answers`, `_Unit.explained`; `tool_explanation_learning.py`:
//! `lookup`, `store`), held through the service's scenes
//! (`tests/fixtures/tool-explanation-service-vectors.json`).

use std::collections::BTreeMap;
use std::collections::btree_map::Entry;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use super::read::snippet;
use super::shapes::{self, Part};
use super::{text, version};
use crate::python;

/// Which prompt a model is asked with: a row learned under another is not said again.
pub const PROMPT_VERSION: i64 = 3;
/// How long a sentence may be, and a sentence with places.
pub const TEXT_MOST: usize = 240;
pub const TEMPLATE_MOST: usize = 400;
/// What a janitor may be told to explain from: everything, everything but the third source
/// (a model that picks among what is known, which this Host has not), or the model alone.
pub const SOURCES_ALL: u8 = 0;
pub const SOURCES_MODEL: u8 = 2;
/// The reasons for which the third source could be asked.
const THIRD_SOURCE: [&str; 2] = ["unknown_program", "unknown_tool"];
/// The tiers an answer comes from, from the cheapest on.
const TIERS: [&str; 4] = ["template", "learned", "jev", "llm"];

/// One learned sentence.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Row {
    pub signature: String,
    /// The reader it was written for.
    pub level: u8,
    /// The sentence: with places for values, or the one text.
    pub template_text: String,
    /// The slots its places name.
    pub slot_names: Vec<String>,
    pub program: String,
    pub action: String,
    /// Who said it: `llm`.
    pub producer: String,
    /// Whether it has places and holds for other values.
    pub parameterised: bool,
    pub prompt_version: i64,
    pub templates_version: Value,
    pub created_at: i64,
    /// How often it answered.
    pub hits: u64,
    pub last_hit_at: Option<i64>,
}

/// The table of what was learned.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Table {
    rows: BTreeMap<(String, u8), Row>,
}

impl Table {
    /// The table that holds these rows.
    pub fn of(rows: impl IntoIterator<Item = Row>) -> Self {
        let rows = rows
            .into_iter()
            .map(|row| ((row.signature.clone(), row.level), row));
        Self {
            rows: rows.collect(),
        }
    }

    /// Every row, by signature and reader.
    pub fn rows(&self) -> impl Iterator<Item = &Row> {
        self.rows.values()
    }

    /// The row that answers for a signature and a reader: the one written for that reader,
    /// else one written for every reader, and only one made under what is current.
    pub fn find(&self, signature: &str, level: u8) -> Option<&Row> {
        let current =
            |row: &&Row| row.prompt_version == PROMPT_VERSION && row.templates_version == version();
        let at = |level: u8| {
            self.rows
                .get(&(signature.to_string(), level))
                .filter(current)
        };
        at(level).or_else(|| at(0))
    }

    /// Keeps a row, in the place of the one that was there for its signature and reader.
    /// What that one had answered is counted on.
    pub fn store(&mut self, mut row: Row) {
        let place = (row.signature.clone(), row.level);
        if let Some(before) = self.rows.get(&place) {
            row.hits = before.hits;
            row.last_hit_at = before.last_hit_at;
        }
        self.rows.insert(place, row);
    }

    /// Counts that a row answered. Answers the row as it is now, to be kept.
    pub fn hit(&mut self, signature: &str, level: u8, now_ms: i64) -> Option<&Row> {
        let row = self.rows.get_mut(&(signature.to_string(), level))?;
        row.hits += 1;
        row.last_hit_at = Some(now_ms);
        Some(row)
    }

    /// Moves what was learned under an earlier name of a part to the name it has now.
    /// Answers what was taken away and what is there now, to be kept.
    pub fn rename(&mut self, old: &str, new: &str, program: &str) -> (Vec<Row>, Vec<Row>) {
        let places: Vec<(String, u8)> = self
            .rows
            .keys()
            .filter(|(signature, _)| signature == old)
            .cloned()
            .collect();
        let (mut gone, mut there) = (Vec::new(), Vec::new());
        for place in places {
            let Some(mut row) = self.rows.remove(&place) else {
                continue;
            };
            gone.push(row.clone());
            // Where the new name has a row already, that one stays.
            if let Entry::Vacant(free) = self.rows.entry((new.to_string(), place.1)) {
                row.signature = new.to_string();
                row.program = program.to_string();
                free.insert(row.clone());
                there.push(row);
            }
        }
        (gone, there)
    }

    /// Takes away the rows a later library or prompt left behind long enough ago, and of the
    /// rows for one text alone all but the most recently used. Answers what was taken.
    pub fn prune(&mut self, now_ms: i64, stale_after_ms: i64, exact_most: usize) -> Vec<Row> {
        let used = |row: &Row| row.last_hit_at.unwrap_or(row.created_at);
        let current =
            |row: &Row| row.prompt_version == PROMPT_VERSION && row.templates_version == version();
        let mut exact: Vec<(i64, (String, u8))> = Vec::new();
        let mut going: Vec<(String, u8)> = Vec::new();
        for (place, row) in &self.rows {
            if !current(row) && used(row) < now_ms - stale_after_ms {
                going.push(place.clone());
            } else if !row.parameterised {
                exact.push((used(row), place.clone()));
            }
        }
        exact.sort();
        let over = exact.len().saturating_sub(exact_most);
        going.extend(exact.into_iter().take(over).map(|(_, place)| place));
        going
            .iter()
            .filter_map(|place| self.rows.remove(place))
            .collect()
    }
}

/// What a developer is told of where an answer came from. What a route names is already
/// read, with secrets taken out.
pub fn provenance(route: &Value, confidence: Option<f64>, reason: &str) -> Map<String, Value> {
    let mut told = Map::new();
    told.insert("template_version".to_string(), version());
    if route.get("template_id").is_some_and(python::truthy) {
        told.insert("template_id".to_string(), route["template_id"].clone());
        let parameters = route
            .get("parameters")
            .cloned()
            .unwrap_or_else(|| json!({}));
        told.insert("parameters".to_string(), parameters);
    }
    if let Some(confidence) = confidence {
        told.insert("confidence".to_string(), json!(confidence));
    }
    if !reason.is_empty() {
        told.insert("fallback_reason".to_string(), text(reason));
    }
    told
}

/// The answer of the templates for a part: its sentence, and that it is scripted.
pub fn scripted(said: &str, route: &Value) -> Value {
    json!({
        "text": said, "tier": "template", "source": "scripted",
        "provenance": provenance(route, Some(1.0), ""),
    })
}

/// The answer of the table for a part: what was learned under its shape, else under its
/// whole text, else under a name an earlier version gave it (which is then moved). `None`:
/// nothing was learned that can be said of it.
pub fn learned(part: &Part, table: &Table, level: u8) -> Option<Value> {
    let answer = |said: &str, row: &Row, under: &str| {
        json!({
            "text": said, "tier": "learned", "source": "llm",
            "provenance": {
                "template_version": version(), "tier": "learned",
                "parameterised": row.parameterised,
            },
            "hit": under, "hit_level": row.level,
        })
    };
    let said_by = |row: &Row| {
        let said = if row.parameterised {
            shapes::render(&row.template_text, &part.slots)?
        } else {
            row.template_text.clone()
        };
        Some(said).filter(|said| !said.is_empty() && said.chars().count() <= TEXT_MOST)
    };
    let filled = |row: &&Row| !row.template_text.is_empty();
    if let Some(row) = table.find(&part.signature, level).filter(filled)
        && let Some(said) = said_by(row)
    {
        return Some(answer(&said, row, &part.signature));
    }
    if part.exact_key != part.signature
        && let Some(row) = table.find(&part.exact_key, level).filter(filled)
        && !row.parameterised
    {
        return Some(answer(&row.template_text, row, &part.exact_key));
    }
    for (old, new) in &part.legacy {
        if let Some(row) = table.find(old, level).filter(filled)
            && let Some(said) = said_by(row)
        {
            let mut found = answer(&said, row, old);
            found["move"] = json!([old, new, part.program]);
            return Some(found);
        }
    }
    None
}

/// One answer for a call from the answers to its parts, in their order.
pub fn assemble(answers: &[Value]) -> Value {
    let said = |answer: &Value, key: &str| answer[key].as_str().unwrap_or_default().to_string();
    let rank = |tier: &str| TIERS.iter().position(|known| *known == tier).unwrap_or(0);
    let tiers: Vec<String> = answers.iter().map(|answer| said(answer, "tier")).collect();
    let worst = tiers
        .iter()
        .max_by_key(|tier| rank(tier))
        .cloned()
        .unwrap_or_default();
    let from = |source: &str| answers.iter().any(|answer| answer["source"] == source);
    let source = ["llm", "jev"]
        .into_iter()
        .find(|source| from(source))
        .unwrap_or("scripted");
    let (words, told) = match answers {
        [only] => (said(only, "text"), only["provenance"].clone()),
        several => {
            let texts: Vec<String> = several.iter().map(|answer| said(answer, "text")).collect();
            let texts: Vec<&str> = texts.iter().map(String::as_str).collect();
            let parts: Vec<Value> = several
                .iter()
                .map(|answer| {
                    let mut told = answer["provenance"]
                        .as_object()
                        .cloned()
                        .unwrap_or_default();
                    told.shift_remove("argument");
                    told.insert("tier".to_string(), answer["tier"].clone());
                    Value::Object(told)
                })
                .collect();
            let told = json!({ "template_version": version(), "tier": worst, "parts": parts });
            (shapes::join(&texts), told)
        }
    };
    json!({
        "status": "ready", "text": words, "source": source,
        "cached": tiers.iter().any(|tier| tier == "learned"),
        "provenance": told, "tier": worst,
    })
}

/// What a model answered, as a sentence and a sentence with places for each thing it was
/// asked about by number. `None`: it cannot be used: it is no table, it is not about
/// exactly what was asked, or a sentence is missing, empty or too long.
pub fn answers(said: &Value, asked: usize) -> Option<Vec<(String, String)>> {
    let said = said.as_object().filter(|said| said.len() == asked)?;
    let mut read = Vec::with_capacity(asked);
    for number in 1..=asked {
        let (sentence, template) = match said.get(&number.to_string())? {
            Value::String(sentence) => (sentence.as_str(), ""),
            Value::Object(answer) => {
                let template = match answer.get("template") {
                    Some(Value::String(template)) => template.as_str(),
                    Some(other) if python::truthy(other) => return None,
                    _ => "",
                };
                (answer.get("text")?.as_str()?, template)
            }
            _ => return None,
        };
        let fits = !python::strip(sentence).is_empty()
            && sentence.chars().count() <= TEXT_MOST
            && template.chars().count() <= TEMPLATE_MOST;
        if !fits {
            return None;
        }
        read.push((sentence.to_string(), template.to_string()));
    }
    Some(read)
}

/// What a model said of a part: the answer, the row it is learned as (under the part's
/// shape where its sentence with places may be said of other values, else under the
/// part's whole text), and why it is kept for the one text alone when it is.
pub fn explained(
    part: &Part,
    route: &Value,
    said: &str,
    template: &str,
    level: u8,
    reason: &str,
    now_ms: i64,
) -> (Value, Row, &'static str) {
    let sentence = snippet(&text(said), TEXT_MOST);
    let sentence = python::strip(&sentence);
    let template = snippet(&text(template), TEMPLATE_MOST);
    let holds = if part.exact {
        Err("exact_part")
    } else {
        shapes::parameterise(sentence, Some(python::strip(&template)), &part.slots)
    };
    let answer = json!({
        "text": sentence, "tier": "llm", "source": "llm",
        "provenance": provenance(route, None, reason),
    });
    let (signature, kept, parameterised) = match holds {
        Ok(template) => (&part.signature, template, true),
        Err(_) => (&part.exact_key, sentence, false),
    };
    let mut slot_names: Vec<String> = if parameterised {
        shapes::placeholders(kept)
            .into_iter()
            .map(str::to_string)
            .collect()
    } else {
        Vec::new()
    };
    slot_names.sort();
    let row = Row {
        signature: signature.clone(),
        level,
        template_text: kept.to_string(),
        slot_names,
        program: part.program.clone(),
        action: route["action"].as_str().unwrap_or_default().to_string(),
        producer: "llm".to_string(),
        parameterised,
        prompt_version: PROMPT_VERSION,
        templates_version: version(),
        created_at: now_ms,
        hits: 0,
        last_hit_at: None,
    };
    (answer, row, holds.err().unwrap_or_default())
}

/// Why a part that no cheaper tier answered goes to the model, as a developer is told it.
/// The third source is not on this Host: where it could have been asked, that is said.
pub fn fallback_reason(route: &Value, level: u8, sources: u8) -> String {
    let own = route["reason"].as_str().unwrap_or_default();
    if level == 0 || sources != SOURCES_ALL {
        return if own.is_empty() {
            "scripted_disabled"
        } else {
            own
        }
        .to_string();
    }
    if THIRD_SOURCE.contains(&own) {
        return "jev_disabled".to_string();
    }
    own.to_string()
}

/// The worst of the tiers a call's parts were answered from: what the call is counted as.
pub fn worst_tier<'a>(tiers: impl IntoIterator<Item = &'a str>) -> &'a str {
    let rank = |tier: &str| TIERS.iter().position(|known| *known == tier).unwrap_or(0);
    tiers
        .into_iter()
        .max_by_key(|tier| rank(tier))
        .unwrap_or_default()
}
