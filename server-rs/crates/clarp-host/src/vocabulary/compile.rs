//! Resolve the focused agent's inputs, compile once for the selected engine,
//! and record the exact audit. Preview takes the same path without the insert.
use super::store::{Result,Run,Desk};
use crate::edge::App;
use clarp_kernel::{
    python,
    vocabulary::{
        self, Pack, book::Book,
        sources::{Sources, term},
    },
};
use serde_json::{Value, json};
use std::path::PathBuf;

struct Context {
    workspace: std::sync::Arc<super::workspace::Cache>,
    book:Book,
    recent:Vec<String>,
    session: String,
    agent: String,
    cwd: PathBuf,
    provider: String,
    model: String,
    names: Vec<String>,
    glossary: String,

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

impl Context {
    fn compile(self)->Value {
        let assigned=self.book.assigned(&self.agent);
        let profile=assigned.as_ref().map(|(id,yes,_)|(id.to_string(),*yes));
        let mut packs=glossary(&self.glossary);
        if let Some((_,_,more))=assigned{packs.extend(more);}
        let mut sources=Sources{recent_transcripts:self.recent,corrections:self.book.corrections(),..Sources::default()};
        sources.agent_names = self.names;
        if profile.as_ref().is_some_and(|(_, yes)| *yes) {
            self.workspace.fill(&mut sources, &self.cwd);
        }
        packs.extend(sources.packs());
        let mut audit =
            vocabulary::compile(&packs, &vocabulary::budget_for(&self.provider, &self.model));
        let pf = profile.map(|(id, _)| id);
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
        audit
    }
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
        "" | "server-default" => ("faster-whisper", app.config.whisper_model.as_str()),
        other => other.split_once(':').unwrap_or((other, "")),
    };
    let (book,recent)=app.ears.vocab.inputs(&app.fleet,session).await?;
    let ctx = Context {
        workspace: app.ears.workspace.clone(),
        book,recent,
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

    };
    let audit=tokio::task::spawn_blocking(move || ctx.compile())
        .await
        .map_err(|_| {
            (
                axum::http::StatusCode::SERVICE_UNAVAILABLE,
                "vocabulary worker ended".into(),
            )
        })?;
    let id=if let Some(trace)=trace{
        let r=Run{session:session.into(),trace:trace.into(),agent:audit["agent_id"].as_str().unwrap_or("").into(),
            profile:audit["profile_id"].as_str().map(str::to_string),provider:provider.into(),model:model.into(),audit:audit.clone(),
            transcript:String::new(),latency:0,now:app.clock.now_ms()};
        app.ears.vocab.record(&app.fleet,r).await.unwrap_or(0)
    }else{0};
    Ok((audit,id))
}

pub async fn stream_run(desk:&Desk,fleet:&crate::fleet::FleetHandle,run:&clarp_kernel::speech_stream::Run<'_>,now:i64)->Option<i64>{
    let audit = json!({"payload":run.keyterms.join(", "),"form":"terms","unit":"terms","capacity":run.capacity,"used":run.keyterms.len(),"rarity_floor":0.0,"dropped":[],"included":run.keyterms.iter().map(|t|json!({"text":t,"pack":"stream","rarity":0.5})).collect::<Vec<_>>()});
    desk.record(fleet,Run{session:run.session.into(),trace:run.trace_id.into(),agent:String::new(),profile:None,
        provider:run.provider.into(),model:run.model.into(),audit,transcript:run.transcript.into(),latency:run.latency_ms,now}).await.ok()
}
