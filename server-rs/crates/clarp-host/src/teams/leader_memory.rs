//! The leader's four decision-memory commands share the Teams desk's durable writer.
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    sync::PoisonError,
};

use clarp_kernel::{leader_memory as rules, python};
use serde_json::{Value, json};

use super::{Desk, Stored, read, write};
use crate::{
    edge::App,
    tools::{ToolAnswer, ToolCall},
    turn_identity,
};

const DECISIONS: &str = "leader-decision";
const APPLICATIONS: &str = "leader-decision-application";
const FACTS: &str = "leader-user-value";
const LIMIT: usize = 5;

fn tags(text: &str) -> Vec<&str> {
    text.split(',')
        .map(python::strip)
        .filter(|tag| !tag.is_empty())
        .collect()
}

pub(super) fn leader(teams: &clarp_kernel::teams::Teams, agent: &str) -> bool {
    teams.values().any(|team| {
        team.archived_at.is_none()
            && team.leader_enabled
            && team.leader_agent_id == agent
            && team.has(agent)
    })
}

pub(super) fn promotion_on_disk(
    disk: &dyn clarp_ports::kv::KeyValue,
) -> Result<i64, clarp_ports::kv::StoreError> {
    Ok(read::<Value>(disk, FACTS)?
        .iter()
        .filter(|row| row["status"] == "promoted")
        .filter_map(|row| row["updated_at"].as_i64())
        .max()
        .unwrap_or_default())
}

fn expand(path: &str) -> PathBuf {
    if let Some(rest) = path.strip_prefix("~/") {
        return crate::config::home().join(rest);
    }
    PathBuf::from(path)
}

fn config_dir() -> PathBuf {
    let home = crate::config::home();
    if cfg!(target_os = "macos") {
        return home.join("Library/Application Support/Clarp");
    }
    std::env::var("XDG_CONFIG_HOME")
        .ok()
        .map(|s| expand(s.trim()))
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| home.join(".config"))
        .join("clarp")
}

fn read_first(paths: impl IntoIterator<Item = PathBuf>) -> std::io::Result<Option<String>> {
    for path in paths {
        match std::fs::read_to_string(path) {
            Ok(text) => return Ok(Some(text)),
            Err(why) if why.kind() == std::io::ErrorKind::NotFound => {}
            Err(why) => return Err(why),
        }
    }
    Ok(None)
}

/// The Host's file is a read-only input. Promoted facts live in the database, even
/// where the document has a manually curated copy of the same facts.
pub(super) fn context(session: &str) -> String {
    let named = std::env::var("CLARP_USER_VALUES_DOC")
        .ok()
        .filter(|s| !s.trim().is_empty());
    let example = |base: &Path| base.join("docs/user-values.example.md");
    let cwd = std::env::current_dir().ok().map(|base| example(&base));
    let installed = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(example));
    let paths = named
        .iter()
        .map(|name| expand(name.trim()))
        .chain([config_dir().join("user-values.md")])
        .chain(cwd)
        .chain(installed);
    let contents = match read_first(paths) {
        Ok(Some(text)) => text,
        Ok(None) => include_str!("user-values.example.md").to_owned(),
        Err(_) => {
            eprintln!("clarp-host: leader user-values document could not be read");
            String::new()
        }
    };
    rules::instruction(session, &contents)
}

struct Ask<'a> {
    verb: &'a str,
    options: BTreeMap<&'a str, &'a str>,
}

impl<'a> Ask<'a> {
    fn parse(args: &'a [String]) -> Result<Self, String> {
        let Some((verb, rest)) = args.split_first() else {
            return Err("a command is required: search, log, apply, user-values".into());
        };
        let (names, required): (&[&str], &[&str]) = match verb.as_str() {
            "search" => (&["question", "tags"], &["question"]),
            "log" => (
                &[
                    "question",
                    "answer",
                    "type",
                    "tags",
                    "risk",
                    "time-horizon",
                    "source-trace",
                    "source-message-id",
                    "source-agent-id",
                ],
                &["question", "answer"],
            ),
            "apply" => (&["decision-id", "reason", "trace-id"], &["decision-id"]),
            "user-values" => (
                &["statement", "category", "decision-id", "tags", "promote"],
                &[],
            ),
            other => return Err(format!("unknown command: {other}")),
        };
        let mut options = BTreeMap::new();
        let mut rest = rest.iter();
        while let Some(raw) = rest.next() {
            let Some(flag) = raw.strip_prefix("--") else {
                return Err(format!("unrecognized arguments: {raw}"));
            };
            let (flag, inline) = flag
                .split_once('=')
                .map_or((flag, None), |(k, v)| (k, Some(v)));
            if !names.contains(&flag) {
                return Err(format!("unrecognized arguments: {raw}"));
            }
            if flag == "promote" {
                if inline.is_some() {
                    return Err("argument --promote: ignored explicit argument".into());
                }
                options.insert(flag, "");
                continue;
            }
            let value = inline
                .or_else(|| {
                    rest.next()
                        .map(String::as_str)
                        .filter(|s| !s.starts_with("--"))
                })
                .ok_or_else(|| format!("argument --{flag}: expected one argument"))?;
            options.insert(flag, value);
        }
        let missing: Vec<_> = required
            .iter()
            .filter(|name| !options.contains_key(*name))
            .map(|name| format!("--{name}"))
            .collect();
        if !missing.is_empty() {
            return Err(format!(
                "the following arguments are required: {}",
                missing.join(", ")
            ));
        }
        Ok(Self { verb, options })
    }

    fn get(&self, key: &str, default: &'a str) -> &'a str {
        self.options.get(key).copied().unwrap_or(default)
    }

    fn word(&self, key: &str) -> &'a str {
        self.get(key, "")
    }
}

fn decision(disk: &dyn clarp_ports::kv::KeyValue, id: &str) -> Result<Option<Value>, String> {
    disk.get(DECISIONS, id)
        .map_err(|why| why.to_string())?
        .map(|raw| serde_json::from_slice(&raw).map_err(|why| why.to_string()))
        .transpose()
}

fn stamp(row: &Value) -> (i64, i64) {
    (
        row["updated_at"].as_i64().unwrap_or_default(),
        row["created_at"].as_i64().unwrap_or_default(),
    )
}

fn text<'a>(row: &'a Value, key: &str) -> &'a str {
    row[key].as_str().unwrap_or_default()
}

fn search(store: &Stored, ask: &Ask<'_>) -> Result<String, String> {
    let question = rules::words(ask.word("question")).to_lowercase();
    let key = rules::question_key(ask.word("question"));
    let snippet: String = question.chars().take(80).collect();
    let named = tags(ask.word("tags"));
    let first = named
        .first()
        .map_or_else(|| "\0".to_string(), |tag| tag.to_lowercase());
    let tag_match = |row: &Value| rules::matches_like(&row["tags"].to_string(), &first);
    let likes =
        |row: &Value, field| rules::matches_like(text(row, field), &snippet) || tag_match(row);
    let mut decisions = read::<Value>(&*store.disk, DECISIONS).map_err(|why| why.to_string())?;
    decisions.retain(|row| {
        row["status"] == "active"
            && (row["question_hash"] == key || likes(row, "canonical_question"))
    });
    decisions.sort_by_key(|row| std::cmp::Reverse(stamp(row)));
    decisions.truncate(LIMIT);
    let mut facts = read::<Value>(&*store.disk, FACTS).map_err(|why| why.to_string())?;
    facts.retain(|row| {
        matches!(text(row, "status"), "candidate" | "promoted") && likes(row, "statement")
    });
    facts.sort_by(|a, b| {
        text(b, "status")
            .cmp(text(a, "status"))
            .then_with(|| {
                b["evidence_count"]
                    .as_i64()
                    .cmp(&a["evidence_count"].as_i64())
            })
            .then_with(|| stamp(b).0.cmp(&stamp(a).0))
    });
    facts.truncate(LIMIT);
    serde_json::to_string_pretty(&json!({"decisions":decisions,"user_value_facts":facts}))
        .map_err(|why| why.to_string())
}

fn keep(store: &mut Stored, table: &str, id: &str, row: &Value) -> Result<(), String> {
    let write = write(table, id, row).map_err(|why| why.to_string())?;
    store
        .disk
        .put_together(&[write])
        .map_err(|why| why.to_string())
}

fn log(store: &mut Stored, ask: &Ask<'_>, at: i64) -> Result<String, String> {
    let question = rules::words(ask.word("question"));
    let key = rules::question_key(&question);
    if read::<Value>(&*store.disk, DECISIONS)
        .map_err(|why| why.to_string())?
        .iter()
        .any(|row| row["status"] == "active" && row["question_hash"] == key)
    {
        return Err("UNIQUE constraint failed: decisions.question_hash".into());
    }
    let id = format!("dec_{}", crate::ids::hex(16));
    let answer = python::strip(ask.word("answer"));
    let row = json!({
        "id":id, "canonical_question":question, "question_hash":key, "context":{},
        "user_answer":answer, "normalized_answer":answer, "decision_type":ask.get("type","workflow"),
        "scope":{}, "tags":tags(ask.word("tags")), "risk_class":ask.get("risk","low"),
        "time_horizon":ask.get("time-horizon","until_changed"), "confidence":1.0, "status":"active",
        "supersedes_id":null, "source_trace":ask.word("source-trace"),
        "source_message_id":ask.word("source-message-id"),"source_agent_id":ask.word("source-agent-id"),
        "created_at":at,"updated_at":at,"expires_at":null,
    });
    keep(store, DECISIONS, &id, &row)?;
    Ok(id)
}

fn apply(store: &mut Stored, ask: &Ask<'_>, at: i64) -> Result<String, String> {
    let named = ask.word("decision-id");
    if decision(&*store.disk, named)?.is_none() {
        return Err("FOREIGN KEY constraint failed".into());
    }
    let id = format!("dapp_{}", crate::ids::hex(16));
    keep(
        store,
        APPLICATIONS,
        &id,
        &json!({
            "application_id":id,"decision_id":named,"task_id":"","run_id":"","trace_id":ask.word("trace-id"),
            "applied_context":{},"outcome":"used","reason":ask.word("reason"),"created_at":at,
        }),
    )?;
    Ok(id)
}

fn user_value(store: &mut Stored, ask: &Ask<'_>, at: i64) -> Result<String, String> {
    let named = ask.word("decision-id");
    let mut statement = rules::words(ask.word("statement"));
    if statement.is_empty() && !named.is_empty() {
        let row =
            decision(&*store.disk, named)?.ok_or_else(|| format!("decision not found: {named}"))?;
        let normalized = python::strip(text(&row, "normalized_answer"));
        statement = rules::words(if normalized.is_empty() {
            text(&row, "user_answer")
        } else {
            normalized
        });
    }
    if statement.is_empty() {
        return Err("statement is required unless --decision-id resolves".into());
    }
    let category = ask.get("category", "preference");
    let promote = ask.options.contains_key("promote");
    let confidence: f64 = if promote { 0.8 } else { 0.7 };
    let old = read::<Value>(&*store.disk, FACTS)
        .map_err(|why| why.to_string())?
        .into_iter()
        .filter(|row| {
            row["category"] == category
                && text(row, "statement").eq_ignore_ascii_case(&statement)
                && matches!(text(row, "status"), "candidate" | "promoted")
        })
        .max_by_key(stamp);
    let (id, mut row) = if let Some(mut row) = old {
        let id = text(&row, "fact_id").to_owned();
        row["evidence_count"] = json!(
            row["evidence_count"]
                .as_u64()
                .unwrap_or(0)
                .saturating_add(1)
        );
        row["confidence"] = json!(
            row["confidence"]
                .as_f64()
                .unwrap_or_default()
                .max(confidence)
        );
        row["updated_at"] = json!(at);
        (id, row)
    } else {
        if !named.is_empty() && decision(&*store.disk, named)?.is_none() {
            return Err("FOREIGN KEY constraint failed".into());
        }
        let id = format!("sf_{}", crate::ids::hex(16));
        let row = json!({
            "fact_id":id,"decision_id":(!named.is_empty()).then_some(named), "statement":statement,
            "category":category,"scope":{},"tags":tags(ask.word("tags")),"evidence_count":1,
            "confidence":confidence,"status":"candidate","created_at":at,"updated_at":at,
        });
        (id, row)
    };
    if promote {
        row["status"] = json!("promoted");
    }
    keep(store, FACTS, &id, &row)?;
    if row["status"] == "promoted" {
        store.latest_promotion = store.latest_promotion.max(at);
    }
    Ok(id)
}

impl Desk {
    pub(crate) fn latest_promotion(&self) -> i64 {
        self.0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .latest_promotion
    }

    fn memory(&self, agent: &str, args: &[String], at: i64) -> ToolAnswer {
        let ask = match Ask::parse(args) {
            Ok(ask) => ask,
            Err(why) => return ToolAnswer::said(2, format!("leader-decision: {why}")),
        };
        let mut store = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        if !leader(&store.teams, agent) {
            return ToolAnswer::said(1, "leader-decision: a current team leader must ask");
        }
        let outcome = match ask.verb {
            "search" => search(&store, &ask),
            "log" => log(&mut store, &ask, at),
            "apply" => apply(&mut store, &ask, at),
            _ => user_value(&mut store, &ask, at),
        };
        match outcome {
            Ok(output) => ToolAnswer::printed(0, output),
            Err(why) => ToolAnswer::said(1, format!("leader-decision: {why}")),
        }
    }
}

pub(crate) async fn run(app: &App, call: &ToolCall, args: &[String]) -> ToolAnswer {
    let denied = || ToolAnswer::said(1, "leader-decision: a current team leader must ask");
    let Some(token) = call.env.get(turn_identity::TURN_ENV) else {
        return denied();
    };
    let (agent, turn) = match turn_identity::claim(app, token).await {
        Ok(Some(claim)) => claim,
        Ok(None) => return denied(),
        Err(why) => return ToolAnswer::said(1, why.0),
    };
    let directory = app.fleet.directory();
    let Some(member) = directory.by_id(&agent) else {
        return denied();
    };
    if member.record.archived_at_ms.is_some()
        || member.record.is_janitor
        || member.agent.require_here().is_err()
    {
        return denied();
    }
    match member.agent.running_trace(turn).await {
        Ok(Some(_)) => {}
        Ok(None) => return denied(),
        Err(why) => return ToolAnswer::said(1, why.0),
    }
    let desk = app.teams.clone();
    let args = args.to_vec();
    let at = app.clock.now_ms();
    tokio::task::spawn_blocking(move || desk.memory(&agent, &args, at))
        .await
        .unwrap_or_else(|_| ToolAnswer::said(1, "leader-decision: team store unavailable"))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use clarp_ports::kv::KeyValueEngine;
    use clarp_store_mem::MemKeyValue;

    fn call(desk: &Desk, actor: &str, args: &[&str], at: i64) -> ToolAnswer {
        let args: Vec<String> = args.iter().map(ToString::to_string).collect();
        desk.memory(actor, &args, at)
    }

    fn scrub(rows: &mut Value, ids: &[(&str, &str)]) {
        for key in ["decisions", "user_value_facts"] {
            for row in rows[key].as_array_mut().unwrap() {
                for value in row.as_object_mut().unwrap().values_mut() {
                    if let Some((_, placeholder)) =
                        ids.iter().find(|(id, _)| value.as_str() == Some(id))
                    {
                        *value = json!(placeholder);
                    }
                }
            }
        }
    }

    #[test]
    fn leader_memory_matches_the_reference_and_keeps_promoted_facts_after_reopen() {
        let vectors: Value = serde_json::from_str(include_str!(
            "../../../clarp-kernel/tests/fixtures/leader-memory-vectors.json"
        ))
        .unwrap();
        for sample in vectors["compact"].as_array().unwrap() {
            assert_eq!(
                rules::compact_user_values(sample["document"].as_str().unwrap()),
                sample["text"]
            );
        }
        let brief = rules::instruction(
            "leader",
            vectors["compact"][2]["document"].as_str().unwrap(),
        );
        let expected = vectors["brief"].as_str().unwrap().replace(
            "python3 scripts/leader_decision.py",
            "clarp-admin leader-decision",
        );
        assert_eq!(brief.trim(), expected.trim());
        assert_eq!(
            rules::question_key(vectors["question"].as_str().unwrap()),
            vectors["question_key"]
        );

        let at = 1_795_000_000_000;
        let engine = MemKeyValue::new();
        let path = Path::new("leader-memory-test");
        let desk = Desk::open(engine.open(path).unwrap()).unwrap();
        let fields = |value: Value| value.as_object().unwrap().clone();
        let team = "a-team";
        desk.create(team.into(), &fields(json!({"name":"Runtime"})), at)
            .unwrap();
        desk.add(team, "leader".into(), at).unwrap();
        desk.add(team, "worker".into(), at).unwrap();
        desk.change(team, &fields(json!({"leader":"leader"})), at)
            .unwrap();
        assert_ne!(
            call(&desk, "worker", &["search", "--question", ""], at).exit,
            0
        );
        let question = vectors["question"].as_str().unwrap();
        let logged = call(
            &desk,
            "leader",
            &[
                "log",
                "--question",
                question,
                "--answer",
                "  Reuse the proof.  ",
                "--tags",
                "Proof",
            ],
            at,
        );
        assert_eq!(logged.exit, 0, "{}", logged.stderr);
        let decision = logged.stdout.trim();
        assert!(decision.starts_with("dec_"));
        assert_ne!(
            call(
                &desk,
                "leader",
                &["log", "--question", question, "--answer", "other"],
                at
            )
            .exit,
            0
        );
        let applied = call(
            &desk,
            "leader",
            &[
                "apply",
                "--decision-id",
                decision,
                "--reason",
                "same evidence",
                "--trace-id",
                "trace",
            ],
            at,
        );
        assert_eq!(applied.exit, 0);
        assert!(applied.stdout.starts_with("dapp_"));
        let inserted = call(
            &desk,
            "leader",
            &["user-values", "--decision-id", decision, "--tags", "Proof"],
            at,
        );
        assert_eq!(inserted.exit, 0);
        assert_eq!(desk.latest_promotion(), vectors["before"]);
        let fact = inserted.stdout.trim();
        let ids = [(decision, "<decision>"), (fact, "<fact>")];
        let got = call(
            &desk,
            "leader",
            &["search", "--question", "", "--tags", "proof"],
            at,
        );
        let mut candidate: Value = serde_json::from_str(&got.stdout).unwrap();
        scrub(&mut candidate, &ids);
        assert_eq!(candidate, vectors["candidate"]);

        let promoted = call(
            &desk,
            "leader",
            &[
                "user-values",
                "--statement",
                "reuse the proof.",
                "--promote",
            ],
            at + 10,
        );
        assert_eq!(promoted.stdout.trim(), fact);
        assert_eq!(desk.latest_promotion(), vectors["after"]);
        drop(desk);

        let desk = Desk::open(engine.open(path).unwrap()).unwrap();
        assert_eq!(desk.latest_promotion(), vectors["idempotent"]);
        let got = call(
            &desk,
            "leader",
            &[
                "search",
                "--question",
                "not the question",
                "--tags",
                "p%f,missing",
            ],
            at + 20,
        );
        let mut promoted: Value = serde_json::from_str(&got.stdout).unwrap();
        scrub(&mut promoted, &ids);
        assert_eq!(promoted, vectors["promoted"]);
        desk.change(team, &fields(json!({"leader_enabled":false})), at + 20)
            .unwrap();
        assert_ne!(
            call(&desk, "leader", &["search", "--question", ""], at + 20).exit,
            0
        );
    }
}
