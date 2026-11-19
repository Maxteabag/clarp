//! Which judgments justify a quiet report about a working label.
use crate::python;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Evidence {
    pub agent_id: String,
    pub name: String,
    pub packet: Value,
    pub idle_min: Option<i64>,
}

fn reason(choice: &str) -> Option<&'static str> {
    match choice {
        "finished" => Some("the work it describes looks finished"),
        "waiting_for_user" => Some("it looks like it is waiting for your answer"),
        "stalled" => Some("nothing seems to be happening"),
        "different_work" => Some("it seems to be doing something else"),
        _ => None,
    }
}

fn probability(answer: &Value, key: &str) -> Option<f64> {
    match answer["probabilities"].get(key) {
        None => Some(0.0),
        Some(number) => number
            .as_f64()
            .or_else(|| number.as_str()?.trim().parse().ok()),
    }
}

/// A missing question invalidates the batch. Missing probabilities default to zero.
pub fn findings(evidence: &[Evidence], answers: &Value) -> Option<Vec<Value>> {
    let mut found = Vec::new();
    for (index, entry) in evidence.iter().enumerate() {
        let answer = answers.get(format!("l_a{}", index + 1))?;
        let choice = answer["choice"].as_str().unwrap_or_default();
        let accurate = probability(answer, "accurate")?;
        let confidence = probability(answer, choice)?;
        let Some(reason) = reason(choice) else {
            continue;
        };
        if accurate <= 0.30 && confidence >= 0.50 {
            let age = match entry.idle_min {
                None => "a while".into(),
                Some(m) if m < 90 => format!("{m} min"),
                Some(m) => format!("{}h", (m as f64 / 60.0).round_ties_even()),
            };
            let label = entry.packet["label"].as_str().unwrap_or_default();
            let line = format!(
                "{} says '{label}', but {reason} (no change for {age}).",
                entry.name
            );
            found.push(
                json!({"agent_id":entry.agent_id,"name":entry.name,"label":label,"verdict":choice,
                "match":format!("{accurate:.3}").parse::<f64>().ok()?,"line":line}),
            );
        }
    }
    Some(found)
}

/// The marker survives a restart. Changes in age, ordering and confidence do not repeat a report.
pub fn report(
    session: &str,
    run: &str,
    checked: usize,
    found: &[Value],
    previous: &str,
) -> (String, Option<Value>) {
    let mut triples: Vec<_> = found
        .iter()
        .map(|row| {
            ["agent_id", "label", "verdict"].map(|key| row[key].as_str().unwrap_or_default())
        })
        .collect();
    triples.sort();
    let fingerprint = format!("{:x}", Sha256::digest(python::dumps_ascii(&json!(triples))));
    let count = found.len();
    if count == 0 || fingerprint == previous {
        return (fingerprint, None);
    }
    let first = found[0]["line"].as_str().unwrap_or_default();
    let (title, summary) = if count == 1 {
        ("1 working label looks wrong".to_string(), first.to_string())
    } else {
        (
            format!("{count} working labels look wrong"),
            format!("{first} And {} more.", count - 1),
        )
    };
    let lines = found
        .iter()
        .map(|row| format!("- {}", row["line"].as_str().unwrap_or_default()))
        .collect::<Vec<_>>()
        .join("\n");
    let mut items = found.to_vec();
    for item in &mut items {
        if let Some(fields) = item.as_object_mut() {
            fields.remove("line");
        }
    }
    let hash = format!("{:x}", Sha256::digest(run.as_bytes()));
    (
        fingerprint,
        Some(
            json!({"type":"document","status":"completed","session":session,"title":title,
        "artifact_id":format!("label-audit-{}",&hash[..40]),"reference_id":format!("label-audit:{run}"),"summary":summary,
        "payload":{"attention_kind":"label_mismatch","run_id":run,"checked":checked,"items":items,
        "content":format!("Checked {checked} working labels. These look out of date; nothing was changed.\n\n{lines}")}}),
        ),
    )
}

/// A single Jev choice request shares the evidence across all questions.
pub fn batch(entries: &[Evidence]) -> Option<Value> {
    use std::sync::LazyLock;
    #[allow(clippy::expect_used)]
    static QUESTION: LazyLock<Value> = LazyLock::new(|| {
        serde_json::from_str(include_str!("label-question.json")).expect("reference label question")
    });
    if entries.is_empty() {
        return None;
    }
    let mut agents = serde_json::Map::new();
    let mut questions = serde_json::Map::new();
    for (number, entry) in (1..).zip(entries) {
        let key = format!("a{number}");
        let mut q = QUESTION.clone();
        q["instructions"] = json!(
            q["instructions"]
                .as_str()?
                .replace("agents.a1", &format!("agents.{key}"))
        );
        questions.insert(format!("l_{key}"), q);
        agents.insert(key, entry.packet.clone());
    }
    Some(json!({"model":"jev-latest","state":{"agents":agents},"questions":questions}))
}

pub const JUDGE_SECONDS: u64 = 15;

/// Apply the heartbeat's active hours using the Host's local time at this check.
pub fn quiet(span: &str, zone: &str, now: i64) -> bool {
    let Ok(at) = jiff::Timestamp::from_millisecond(now) else {
        return false;
    };
    let local = crate::oracle::threads::zone_named(zone).to_datetime(at);
    crate::heartbeat::outside_active_hours(span, local.hour() as u32 * 60 + local.minute() as u32)
}

pub fn summary(checked: usize, found: usize, filed: bool) -> String {
    let repeat = if found > 0 && !filed {
        " (already reported)"
    } else {
        ""
    };
    format!("Checked {checked} working labels; {found} look wrong{repeat}")
}
