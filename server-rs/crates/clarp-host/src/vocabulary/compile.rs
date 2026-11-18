//! Resolve the focused agent's inputs, compile once for the selected engine,
//! and record the exact audit. Preview takes the same path without the insert.
use super::store::{self, Result, unavailable};
use crate::edge::App;
use clarp_kernel::{
    python,
    vocabulary::{
        self, Pack, Term,
        sources::{Sources, term},
    },
};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

struct Context {
    root: PathBuf,
    session: String,
    agent: String,
    cwd: PathBuf,
    provider: String,
    model: String,
    names: Vec<String>,
    glossary: String,
    trace: Option<String>,
    now: i64,
}

fn glossary(raw: &str) -> Vec<Pack> {
    let lines: Vec<_> = python::splitlines(raw)
        .into_iter()
        .map(|s| {
            s.split('#')
                .next()
                .unwrap_or("")
                .trim_matches([' ', ',', ';', '\t'])
        })
        .filter(|s| !s.is_empty())
        .collect();
    let text = lines.join("\n");
    let terms: Vec<_> = python::first(&text, 2000)
        .lines()
        .map(python::strip)
        .filter(|s| !s.is_empty())
        .map(|s| term(s, "glossary"))
        .collect();
    if terms.is_empty() {
        vec![]
    } else {
        vec![Pack {
            name: "glossary".into(),
            terms,
            enabled: true,
            floor: 3,
            priority: 1.5,
        }]
    }
}

fn static_packs(db: &Connection, pf: &str) -> Result<Vec<Pack>> {
    let mut query=db.prepare("SELECT p.pack_id,p.name,p.kind,p.priority,p.floor,p.enabled FROM member m JOIN pack p USING(pack_id) WHERE m.profile_id=? ORDER BY m.position,p.name").map_err(unavailable)?;
    let mut packs = Vec::new();
    let mut terms=db.prepare("SELECT text,rarity,say_as,often_heard_as FROM term WHERE pack_id=? ORDER BY rarity DESC,term_id").map_err(unavailable)?;
    let rows = query
        .query_map([pf], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(2)?,
                Pack {
                    name: r.get(1)?,
                    priority: r.get(3)?,
                    floor: r.get(4)?,
                    enabled: r.get(5)?,
                    terms: Vec::new(),
                },
            ))
        })
        .map_err(unavailable)?;
    for r in rows {
        let (id, kind, mut pack) = r.map_err(unavailable)?;
        if kind == "static" {
            pack.terms = terms
                .query_map([id], |r| {
                    Ok(Term {
                        text: r.get(0)?,
                        pack: pack.name.clone(),
                        rarity: r.get(1)?,
                        recency: 0.0,
                        say_as: Some(r.get::<_, String>(2)?).filter(|s| !s.is_empty()),
                        confusable: !r.get::<_, String>(3)?.trim().is_empty(),
                    })
                })
                .map_err(unavailable)?
                .collect::<rusqlite::Result<_>>()
                .map_err(unavailable)?;
        }
        packs.push(pack);
    }
    Ok(packs)
}

fn inputs(db: &Connection, session: &str) -> Result<Sources> {
    let mut s = Sources::default();
    if !session.is_empty() {
        let mut qry=db.prepare("SELECT transcript FROM run WHERE session=? AND transcript!='' ORDER BY run_id DESC LIMIT 3").map_err(unavailable)?;
        s.recent_transcripts = qry
            .query_map([session], |r| r.get(0))
            .map_err(unavailable)?
            .collect::<rusqlite::Result<_>>()
            .map_err(unavailable)?;
    }
    let mut qry=db.prepare("SELECT text,often_heard_as FROM term JOIN pack USING(pack_id) WHERE enabled AND often_heard_as!='' ORDER BY created_at DESC,term_id LIMIT 200").map_err(unavailable)?;
    let rows = qry
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
        .map_err(unavailable)?;
    for r in rows {
        let (intended, heard) = r.map_err(unavailable)?;
        s.corrections.extend(
            heard
                .split([',', ';'])
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(|s| (s.into(), intended.clone())),
        );
    }
    Ok(s)
}

impl Context {
    fn compile(self) -> Result<(Value, i64)> {
        let mut db = store::open(&self.root)?;
        let tx = db.transaction().map_err(unavailable)?;
        let profile=tx.query_row("SELECT profile_id,harvests FROM assignment JOIN profile USING(profile_id) WHERE agent_id=?",[&self.agent],|r|Ok((r.get::<_,String>(0)?,r.get::<_,bool>(1)?))).optional().map_err(unavailable)?;
        let mut packs = glossary(&self.glossary);
        let mut sources = inputs(&tx, &self.session)?;
        if let Some((id, _)) = &profile {
            packs.extend(static_packs(&tx, id)?);
        }
        tx.commit().map_err(unavailable)?;
        sources.agent_names = self.names;
        if profile.as_ref().is_some_and(|(_, yes)| *yes) {
            super::workspace::fill(&mut sources, &self.cwd);
        }
        packs.extend(sources.packs());
        let mut audit =
            vocabulary::compile(&packs, &vocabulary::budget_for(&self.provider, &self.model));
        let pf = profile.map(|(id, _)| id);
        let id = if let Some(trace) = &self.trace {
            insert(
                &db,
                &self.session,
                trace,
                &self.agent,
                pf.as_deref(),
                &self.provider,
                &self.model,
                &audit,
                "",
                0,
                self.now,
            )
            .unwrap_or(0)
        } else {
            0
        };
        // The preview counts every pack in first-appearance order, included terms before drops.
        let mut totals = serde_json::Map::new();
        for k in ["included", "dropped"] {
            for t in audit[k].as_array().into_iter().flatten() {
                let name = t["pack"].as_str().unwrap_or("");
                let entry = totals
                    .entry(name)
                    .or_insert_with(|| json!({"pack":name,"included":0,"dropped":0}));
                entry[k] = json!(entry[k].as_u64().unwrap_or(0) + 1);
            }
        }
        audit["packs"] = json!(totals.into_values().collect::<Vec<_>>());
        audit["agent_id"] = json!(self.agent);
        audit["session"] = json!(self.session);
        audit["provider"] = json!(self.provider);
        audit["model"] = json!(self.model);
        audit["profile_id"] = json!(pf);
        Ok((audit, id))
    }
}

#[allow(clippy::too_many_arguments)]
fn insert(
    db: &Connection,
    session: &str,
    trace: &str,
    agent: &str,
    profile: Option<&str>,
    provider: &str,
    model: &str,
    audit: &Value,
    transcript: &str,
    latency: i64,
    now: i64,
) -> Result<i64> {
    db.execute("INSERT INTO run(session,trace_id,agent_id,profile_id,provider,model,audit,transcript,latency_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",params![session,trace,agent,profile,provider,model,audit.to_string(),transcript,latency,now]).map_err(unavailable)?;
    Ok(db.last_insert_rowid())
}

pub async fn compile(
    app: &App,
    session: &str,
    selected: &str,
    delegated: bool,
    trace: Option<&str>,
) -> Result<(Value, i64)> {
    let dir = app.fleet.directory();
    let record = dir.by_session(session).map(|m| &m.record);
    let names_on = dir
        .setting("transcription.guidance.delegation_names")
        .is_none_or(|s| {
            matches!(
                s.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        });
    let names = if delegated && names_on {
        dir.members
            .iter()
            .filter(|m| m.record.deleted_at_ms.is_none() && m.record.archived_at_ms.is_none())
            .map(|m| m.record.persona.clone())
            .collect()
    } else {
        Vec::new()
    };
    let (provider, model) = match selected.trim() {
        "" | "server-default" | "local" => ("faster-whisper", app.config.whisper_model.as_str()),
        other => other.split_once(':').unwrap_or((other, "")),
    };
    let ctx = Context {
        root: app.config.data_dir.clone(),
        agent: record.map_or_else(String::new, |r| r.agent_id.clone()),
        session: session.into(),
        cwd: record.map_or_else(PathBuf::new, |r| PathBuf::from(&r.cwd)),
        names,
        provider: provider.into(),
        model: model.into(),
        glossary: if delegated {
            String::new()
        } else {
            dir.setting("transcription.guidance.glossary")
                .unwrap_or("")
                .into()
        },
        trace: trace.map(str::to_string),
        now: app.clock.now_ms(),
    };
    tokio::task::spawn_blocking(move || ctx.compile())
        .await
        .map_err(|_| {
            (
                axum::http::StatusCode::SERVICE_UNAVAILABLE,
                "vocabulary worker ended".into(),
            )
        })?
}

pub async fn finish(root: PathBuf, id: i64, transcript: String, latency: i64) {
    if id == 0 {
        return;
    }
    let _ = tokio::task::spawn_blocking(move || {
        let db = store::open(&root)?;
        db.execute(
            "UPDATE run SET transcript=?,latency_ms=? WHERE run_id=?",
            params![transcript, latency, id],
        )
        .map_err(unavailable)
    })
    .await;
}

pub fn stream_run(
    root: &Path,
    run: &clarp_kernel::speech_stream::Run<'_>,
    now: i64,
) -> Option<i64> {
    let db = store::open(root).ok()?;
    let audit = json!({"payload":run.keyterms.join(", "),"form":"terms","unit":"terms","capacity":run.capacity,"used":run.keyterms.len(),"rarity_floor":0.0,"dropped":[],"included":run.keyterms.iter().map(|t|json!({"text":t,"pack":"stream","rarity":0.5})).collect::<Vec<_>>()});
    insert(
        &db,
        run.session,
        run.trace_id,
        "",
        None,
        run.provider,
        run.model,
        &audit,
        run.transcript,
        run.latency_ms,
        now,
    )
    .ok()
}
