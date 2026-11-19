//! Evidence for a label check. Looking at the fleet borrows its published in-memory views.
//! Only after selection may the checker ask an actor to read its conversation.
use crate::{agent::Unavailable, fleet::Directory, wire};
use clarp_actor::conversation::Which;
use clarp_kernel::helpers;
use clarp_kernel::janitors::{
    label_audit::Evidence,
    label_evidence::{Candidate, evidence, selected},
};
use serde_json::{Value, json};
use std::collections::HashMap;

#[derive(Default)]
struct Children {
    sessions: Vec<String>,
    working: Vec<helpers::Working>,
    details: Vec<Value>,
}

fn roster(directory: &Directory) -> Vec<Candidate> {
    let mut ordered: Vec<_> = directory.members.iter().collect();
    ordered.sort_by_key(|member| member.record.created_at_ms);
    let mut children: HashMap<&str, Children> = HashMap::new();
    for member in &ordered {
        let record = &member.record;
        let Some(parent) = record.parent_agent_id.as_deref() else {
            continue;
        };
        let family = children.entry(parent).or_default();
        if record.is_helper() {
            family.sessions.push(record.session.clone());
        }
        if record.helper_state.as_deref() != Some("running") {
            continue;
        }
        let view = member.agent.view();
        let activity = wire::activity(record, &view.status);
        let text = view.status.label.trim();
        let text = if text.is_empty() {
            activity["summary"].as_str().unwrap_or_default().trim()
        } else {
            text
        };
        let state = wire::state(record, &view.status, view.backend, view.ended.as_ref());
        let bucket = if state["detail"]["account_recovery"].is_object() {
            "waiting"
        } else {
            helpers::bucket(state["kind"].as_str().unwrap_or_default())
        };
        family.working.push(helpers::Working {
            session: record.session.clone(),
            text: text.to_owned(),
            bucket,
        });
        family
            .details
            .push(json!({"session":record.session,"bucket":bucket,"text":text,"ts":state["ts"]}));
    }
    let mut facts = Vec::new();
    for member in ordered {
        let record = &member.record;
        if record.is_janitor || record.archived_at_ms.is_some() {
            continue;
        }
        let view = member.agent.view();
        let family = children
            .remove(record.agent_id.as_str())
            .unwrap_or_default();
        let names: Vec<_> = family.sessions.iter().map(String::as_str).collect();
        let mut jobs: Vec<_> = view
            .jobs
            .iter()
            .filter(|j| j.status.is_active() && !view.provider_mirrors.contains_key(&j.id) && wire::mirrored_helper(j, &names).is_none())
            .collect();
        jobs.sort_by_key(|j| j.started_at_ms);
        let processes: Vec<_> = jobs
            .iter()
            .map(|j| helpers::Process {
                title: j.title.clone(),
                progress: j.progress_text.clone(),
                active_at_ms: j.progress_at_ms.unwrap_or(j.started_at_ms),
            })
            .collect();
        let state = wire::state(record, &view.status, view.backend, view.ended.as_ref());
        let state_at = wire::state_since(record, &view.status);
        let ended_at = view.status.last.as_ref().map(|l| l.ended_at_ms);
        let idle_since = view
            .jobs
            .iter()
            .map(|j| j.updated_at_ms)
            .chain(ended_at)
            .fold(state_at.max(record.created_at_ms), i64::max);
        let detail = &state["detail"];
        let detail = detail["message"]
            .as_str()
            .filter(|s| !s.is_empty())
            .or_else(|| detail["summary"].as_str())
            .unwrap_or_default();
        let label = view.status.label.trim();
        facts.push(Candidate{
            agent_id:record.agent_id.clone(),name:if record.persona.is_empty(){record.session.clone()}else{record.persona.clone()},
            excluded:false,busy:view.status.live.is_some() || !view.status.waiting.is_empty(),idle_since,
            label:if label.is_empty(){helpers::work_summary(&family.working,&processes)}else{label.to_owned()},
            state:state["kind"].as_str().unwrap_or_default().into(),detail:detail.into(),state_at,ended_at,
            jobs:jobs.into_iter().map(|j|json!({"title":j.title,"kind":j.kind,"progress_text":j.progress_text,"active_at":j.progress_at_ms.unwrap_or(j.started_at_ms),"heartbeat_at":j.heartbeat_at_ms.unwrap_or(j.updated_at_ms)})).collect(),helpers:family.details,
        });
    }
    facts
}

async fn project<Fut: std::future::Future<Output = Result<Vec<Value>, Unavailable>> + Send>(
    facts: Vec<Candidate>,
    now: i64,
    mut history: impl FnMut(String) -> Fut + Send,
) -> Result<Vec<Evidence>, Unavailable> {
    let mut result = Vec::new();
    for c in selected(facts, now) {
        let messages = history(c.agent_id.clone()).await?;
        result.push(evidence(c, now, &messages));
    }
    Ok(result)
}

/// A scan reads at most three current-conversation entries for each of the selected twelve.
pub async fn candidates(directory: &Directory, now: i64) -> Result<Vec<Evidence>, Unavailable> {
    project(roster(directory), now, |id: String| async move {
        let member = directory
            .named(&id)
            .ok_or_else(|| Unavailable("label check target disappeared".into()))?;
        let page = member.agent.tail(3, Which::All).await?;
        Ok(page.entries.iter().map(wire::row).collect())
    })
    .await
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn history_is_opened_only_for_the_twelve_oldest_labels() {
        let facts = (0..13)
            .rev()
            .map(|i| Candidate {
                agent_id: i.to_string(),
                name: "Example".into(),
                busy: false,
                excluded: false,
                idle_since: i,
                label: "Review".into(),
                state: "done".into(),
                detail: String::new(),
                state_at: 0,
                ended_at: None,
                jobs: vec![],
                helpers: vec![],
            })
            .collect();
        let mut opened = Vec::new();
        let result = project(facts, 1_000_000, |id: String| {
            opened.push(id);
            std::future::ready(Ok(vec![json!({"role":"user","text":"a sample"})]))
        })
        .await;
        assert_eq!(opened, (0..12).map(|i| i.to_string()).collect::<Vec<_>>());
        assert!(result.is_ok_and(|entries| {
            entries.len() == 12
                && entries
                    .iter()
                    .all(|e| e.packet["recent_messages"][0]["text"] == "a sample")
        }));
    }
}

/// The master switch and the labels switch are both required before reading a key.
pub fn site_enabled(app: &crate::edge::App) -> bool {
    let dir = app.fleet.directory();
    dir.setting("judgments.enabled") == Some("true")
        && dir.setting("judgments.labels") == Some("true")
        && app.ears.key("typesafe").is_some()
}

const DIAGNOSTIC: &str = "label-judgment-diagnostic";
const CURSOR: &str = "label-judgment-cursor";
const CAPACITY: u64 = 100;

/// There is one writer, the claimed lane. The cursor and its bounded slot are committed
/// together. The 101st call replaces the first; no diagnostic scan is needed.
async fn diagnostic(app: &crate::edge::App, mut row: Value) -> Result<(), Unavailable> {
    let prior = app.fleet.kept(CURSOR, "labels".into()).await?;
    let next = match prior {
        None => 0,
        Some(bytes) => serde_json::from_slice::<u64>(&bytes)
            .map_err(|_| Unavailable("invalid label diagnostic cursor".into()))?,
    };
    row["sequence"] = json!(next);
    app.fleet
        .keep_together(vec![
            (
                DIAGNOSTIC,
                (next % CAPACITY).to_string(),
                row.to_string().into_bytes(),
            ),
            (
                CURSOR,
                "labels".into(),
                next.wrapping_add(1).to_string().into_bytes(),
            ),
        ])
        .await
}

use crate::vocabulary::judge;
use clarp_kernel::janitors::label_audit::{JUDGE_SECONDS, batch, findings};

/// Ask the labels site once. No other provider is eligible on a failed judgment.
pub async fn judge_labels(
    app: &crate::edge::App,
    entries: &[Evidence],
    run: &str,
) -> Option<Vec<Value>> {
    let body = batch(entries)?;
    if !site_enabled(app) {
        return None;
    }
    let key = app.ears.key("typesafe")?;
    let breaker = &app.ears.judge;
    let began = std::time::Instant::now();
    let clipped = |v: &Value, n| {
        clarp_kernel::python::dumps(v)
            .chars()
            .take(n)
            .collect::<String>()
    };
    let mut row = json!({"run_id":run,"site":"labels","created_at":app.clock.now_ms(),
        "state_json":clipped(&body["state"],4000),"question_json":clipped(&body["questions"],2000),
        "question_ids":(1..=entries.len()).map(|i|format!("l_a{i}")).collect::<Vec<_>>().join(","),
        "error":"","outcome":"","answers_json":"{}","input_tokens":0,"output_tokens":0});
    let found = if breaker.remaining() > 0.0 {
        row["error"] = json!("breaker open");
        None
    } else {
        match judge::post(judge::endpoint(app), key, body, JUDGE_SECONDS * 1000).await {
            Ok(response) => {
                let result = findings(entries, &response["answers"]);
                breaker.record(result.is_some(), false);
                if let Some(items) = &result {
                    row["answers_json"] = json!(clipped(&response["answers"], 4000));
                    for field in ["input_tokens", "output_tokens"] {
                        row[field] = json!(response["usage"][field].as_i64().unwrap_or(0));
                    }
                    let outcome: Vec<_> = entries
                        .iter()
                        .enumerate()
                        .map(|(i, e)| {
                            let verdict = items
                                .iter()
                                .find(|v| v["agent_id"] == e.agent_id)
                                .and_then(|v| v["verdict"].as_str());
                            format!(
                                "a{}={}",
                                i + 1,
                                verdict.map_or_else(|| "ok".into(), |v| format!("mismatch:{v}"))
                            )
                        })
                        .collect();
                    row["outcome"] = json!(outcome.join(","));
                } else {
                    row["error"] = json!("invalid judgment response");
                }
                result
            }
            Err(billing) => {
                breaker.record(false, billing);
                row["error"] = json!(if billing {
                    "billing refusal"
                } else {
                    "judgment unavailable"
                });
                None
            }
        }
    };
    row["fallback_used"] = json!(found.is_none());
    row["latency_ms"] = json!(began.elapsed().as_millis() as u64);
    if let Err(e) = diagnostic(app, row).await {
        app.fleet.unkept(DIAGNOSTIC, &e.0);
    }
    found
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod site_tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read, Write};
    #[tokio::test]
    async fn a_loopback_judge_receives_one_batch_and_returns_each_choice() {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let url = format!("http://{}/judge", listener.local_addr().unwrap());
        let peer = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(&mut stream);
            let mut lines = Vec::new();
            loop {
                let mut line = String::new();
                assert!(reader.read_line(&mut line).unwrap() > 0);
                if line == "\r\n" {
                    break;
                }
                lines.push(line.trim().to_lowercase());
            }
            assert!(lines.iter().any(|l| l == "authorization: bearer demo"));
            assert_eq!(lines[0], "post /judge http/1.1");
            let length = lines
                .iter()
                .find_map(|l| l.strip_prefix("content-length: "))
                .unwrap()
                .parse()
                .unwrap();
            let mut bytes = vec![0; length];
            reader.read_exact(&mut bytes).unwrap();
            let request: Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(request["questions"].as_object().unwrap().len(), 2);
            let body =
                json!({"answers":{"l_a1":{"choice":"finished","probabilities":{"finished":1}},
                "l_a2":{"choice":"accurate","probabilities":{"accurate":1}}}})
                .to_string();
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
            request
        });
        let entries: Vec<_> = ["one", "two"]
            .into_iter()
            .map(|id| Evidence {
                agent_id: id.into(),
                name: id.into(),
                packet: json!({"label":"sample"}),
                idle_min: None,
            })
            .collect();
        let response = judge::post(
            url,
            "demo".into(),
            batch(&entries).unwrap(),
            JUDGE_SECONDS * 1000,
        )
        .await
        .unwrap();
        let found = findings(&entries, &response["answers"]).unwrap();
        assert_eq!(found.len(), 1);
        assert_eq!(found[0]["agent_id"], "one");
        assert_eq!(
            peer.join().unwrap()["state"]["agents"],
            json!({"a1":{"label":"sample"},"a2":{"label":"sample"}})
        );
    }
}

use crate::edge::App;
use clarp_kernel::janitors::{adaptive, label_audit, quota};
const ROLE: &str = "label-auditor";
const CADENCE: &str = "label-checker-cadence";
const LAST: &str = "label-checker-last-check";
const MARK: &str = "label-checker-fingerprint";

#[derive(Default)]
pub struct Desk {
    armed: std::sync::Mutex<Option<(i64, u64)>>,
    looking: std::sync::Arc<tokio::sync::Mutex<()>>,
}

/// Called by the shared Janitor runner. No label-specific timer or polling task.
pub(crate) fn look(app: &App) {
    let d = &app.label_checker;
    let changes = app.janitors.changes();
    let now = app.clock.now_ms();
    let armed = *d
        .armed
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if !quota::due(armed, now, changes) {
        return;
    }
    let Ok(guard) = d.looking.clone().try_lock_owned() else {
        return;
    };
    let app = app.clone();
    tokio::spawn(async move {
        let _guard = guard;
        let next = match pass(&app, now).await {
            Ok(next) => next,
            Err(why) => {
                app.fleet.unkept(ROLE, &why.0);
                now + 15_000
            }
        };
        *app.label_checker
            .armed
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some((next, changes));
    });
}

async fn stored<T: serde::de::DeserializeOwned>(
    app: &App,
    kind: &'static str,
) -> Result<Option<T>, Unavailable> {
    app.fleet
        .kept(kind, ROLE.into())
        .await?
        .map(|raw| {
            serde_json::from_slice(&raw).map_err(|_| Unavailable(format!("invalid {kind} record")))
        })
        .transpose()
}

fn row(kind: &'static str, value: &impl serde::Serialize) -> crate::janitors::Row {
    (
        kind,
        ROLE.to_owned(),
        serde_json::to_vec(value).unwrap_or_default(),
    )
}

async fn pass(app: &App, now: i64) -> Result<i64, Unavailable> {
    let owner = app
        .janitors
        .read(app, |book, world, _| {
            let id = book.resolve(ROLE, None, world).ok()??;
            let config = book.configs.get(&id)?;
            let opts = &config.options;
            Some((
                id,
                config.generation,
                (
                    opts.get("interval_seconds")
                        .and_then(Value::as_i64)
                        .unwrap_or(3600),
                    opts.get("max_interval_seconds")
                        .and_then(Value::as_i64)
                        .unwrap_or(86400),
                ),
            ))
        })
        .await?;
    when_enabled(owner, |owner| active_pass(app, now, owner)).await
}

type Owner = (String, u64, (i64, i64));

/// A missing subscriber (including a paused one) stays asleep until the book changes.
/// All history access and storage effects belong to the admitted pass.
async fn when_enabled<F: std::future::Future<Output = Result<i64, Unavailable>>>(
    owner: Option<Owner>,
    active: impl FnOnce(Owner) -> F,
) -> Result<i64, Unavailable> {
    match owner {
        Some(owner) => active(owner).await,
        None => Ok(i64::MAX),
    }
}

async fn active_pass(
    app: &App,
    now: i64,
    (id, generation, bounds): Owner,
) -> Result<i64, Unavailable> {
    if !site_enabled(app) {
        return Ok(now + 15_000);
    }
    let mut lane = quota::Lane::of(stored(app, CADENCE).await?, &id, generation);
    lane.last_check = stored(app, LAST).await?;
    if now < lane.next_run(bounds.0) {
        return Ok(lane.next_run(bounds.0));
    }
    lane.start(bounds, now);
    lane.last_check = None; // The last check is its own record across ownership changes.
    let directory = app.fleet.directory();
    let entries = candidates(&directory, now).await?;
    if entries.is_empty() {
        lane.record(bounds, (adaptive::IDLE, "Nothing shown as working"), now);
        app.fleet
            .keep_together(vec![row(LAST, &now), row(CADENCE, &lane)])
            .await?;
        return Ok(lane.next_run(bounds.0));
    }
    let Some(member) = directory.janitor(&id) else {
        return Ok(now);
    };
    let packets = batch(&entries)
        .map(|b| b["state"]["agents"].clone())
        .unwrap_or_default();
    let request = json!(quota::digest(&json!([id, now])));
    let context = json!({"input_hash":quota::digest(&packets),"target_count":entries.len()});
    let run = app
        .janitors
        .with_kept(app, |book, world, at| {
            if !book
                .configs
                .get(&id)
                .is_some_and(|c| c.enabled && c.generation == generation)
            {
                return (None, vec![], vec![]);
            }
            let run = book
                .begin_run_of(
                    Some(id.clone()),
                    ROLE,
                    &request,
                    Some(&context),
                    None,
                    world,
                    at,
                )
                .ok()
                .flatten()
                .and_then(|r| {
                    let name = r["run_id"].as_str()?;
                    book.claim_run(name, world, at).then(|| name.to_owned())
                });
            if run.is_none() {
                lane.record(bounds, (adaptive::FAILED, "The check could not start"), now);
            }
            (run, vec![row(LAST, &now), row(CADENCE, &lane)], vec![])
        })
        .await?;
    let Some(run) = run else {
        return Ok(lane.next_run(bounds.0));
    };
    let found = judge_labels(app, &entries, &run).await;
    let mut extra = vec![];
    let (status, activity, summary, result, error) = if let Some(found) = found {
        let prior = stored::<String>(app, MARK).await?.unwrap_or_default();
        let (mark, doc) =
            label_audit::report(&member.record.session, &run, entries.len(), &found, &prior);
        if let Some(d) = &doc
            && !app.janitors.label_document(app, &run, member, d).await?
        {
            return Ok(now);
        }
        let summary = label_audit::summary(entries.len(), found.len(), doc.is_some());
        extra.push(row(MARK, &mark));
        (
            "completed",
            if doc.is_some() {
                adaptive::WORKED
            } else {
                adaptive::IDLE
            },
            summary.clone(),
            json!({"summary":summary,"item_count":found.len(),"target_count":entries.len()}),
            "",
        )
    } else {
        let why = "Jev did not answer; nothing reported";
        ("failed", adaptive::FAILED, why.to_owned(), Value::Null, why)
    };
    lane.record(bounds, (activity, &summary), now);
    extra.push(row(CADENCE, &lane));
    app.janitors
        .with_kept(app, |book, world, at| {
            if !book.is_current(&run, world, at) {
                return ((), vec![], vec![]);
            }
            let kept = book.complete_run(
                &run,
                status,
                result.as_object().map(|_| &result),
                error,
                Some((activity, &summary)),
                world,
                at,
            );
            ((), if kept == Ok(true) { extra } else { vec![] }, vec![])
        })
        .await?;
    Ok(lane.next_run(bounds.0))
}

pub(crate) async fn show_cadence(app: &App, janitor: &mut Value) {
    if janitor["template_id"] != ROLE {
        return;
    }
    let Ok(Some(lane)) = stored::<quota::Lane>(app, CADENCE).await else {
        return;
    };
    if janitor["agent_id"] != lane.agent_id || janitor["generation"] != lane.generation {
        return;
    }
    let base = janitor["options"]["interval_seconds"]
        .as_i64()
        .unwrap_or(3600);
    let cap = janitor["options"]["max_interval_seconds"].as_i64();
    let next = lane.next_run_at.filter(|_| janitor["enabled"] == true);
    janitor["cadence"] = json!([adaptive::cadence(&lane.cadence, (base, cap), next, ROLE)]);
}

#[cfg(test)]
mod paused_tests {
    use super::*;

    #[tokio::test]
    async fn a_paused_checker_leaves_history_and_storage_unopened_on_repeated_looks() {
        let mut active_passes = 0;
        for _ in 0..4 {
            // This is the boundary used by pass after resolving the Janitor.
            // Even constructing the future that owns the history and store ports is observable.
            let next = when_enabled(None, |_| {
                active_passes += 1;
                std::future::ready(Ok(0))
            })
            .await;
            assert!(matches!(next, Ok(i64::MAX)));
        }
        assert_eq!(active_passes, 0);
    }
}
