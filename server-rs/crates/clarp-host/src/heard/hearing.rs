//! `clarp-admin transcription agent [--session SESSION] [--language CODES] [--engines
//! ENGINES]`: shows or sets how one agent is heard, its own language (one code, or up to
//! three the speaker moves between) and its own engines in the order they are tried (issue
//! 764). With no session, the agent that calls. `default` takes the agent's own setting away,
//! so it is heard like every agent of this Host.
//!
//! The change is the one an app makes with `POST /transcription-providers`, through the same
//! rules and the same words of refusal; an agent may set another agent's hearing as it may set
//! its model (`clarp-admin model --session`). The entry is kept under the agent's session, and
//! the Host keeps it under the agent's id beside it.

use axum::body::Bytes;
use axum::extract::State;
use clarp_backend::SESSION_ENV;
use clarp_kernel::languages;
use clarp_kernel::said::Who;
use serde_json::{Map, Value, json};

use crate::artifact_tool::parse;
use crate::edge::App;
use crate::fleet::Member;
use crate::tools::{ToolAnswer, ToolCall};

pub(crate) const USAGE: &str = "usage: clarp-admin transcription agent [-h] [--session SESSION] \
     [--language LANGUAGE] [--engines ENGINES]";

/// The word that takes an agent's own setting away.
const DEFAULT: &str = "default";

pub(crate) async fn agent(app: &App, call: &ToolCall, args: &[String]) -> ToolAnswer {
    let valued = ["--session", "--language", "--engines"];
    let parsed = match parse(args, &valued, &[], &[]) {
        Ok(parsed) => parsed,
        Err(why) => {
            return ToolAnswer::said(
                2,
                format!("{USAGE}\nclarp-admin transcription agent: error: {why}"),
            );
        }
    };
    let own = call.env.get(SESSION_ENV).map_or("", String::as_str);
    let session = parsed.option("--session").unwrap_or(own).trim().to_string();
    if session.is_empty() {
        return ToolAnswer::said(
            1,
            "no session: pass --session (CLAUDE_PWA_SESSION is unset)",
        );
    }
    let Some(member) = app.member(&session) else {
        return ToolAnswer::said(1, format!("no such agent session: {session}"));
    };
    let mut change = Map::new();
    for (option, key) in [
        ("--language", "agent_languages"),
        ("--engines", "agent_engines"),
    ] {
        if let Some(said) = parsed.option(option) {
            change.insert(key.to_string(), entry(app, &member, said.trim()));
        }
    }
    let changed = !change.is_empty();
    if changed {
        let body = Bytes::from(Value::Object(change).to_string());
        if let Err(refused) = super::set_providers(State(app.clone()), body).await {
            return ToolAnswer::said(
                1,
                format!(
                    "transcription change refused (HTTP {}): {}",
                    refused.status().as_u16(),
                    refused.message()
                ),
            );
        }
    }
    let mut shown = heard_as(app, &member);
    if changed {
        shown["applies"] = json!("next clip");
    }
    match serde_json::to_string_pretty(&shown) {
        Ok(text) => ToolAnswer::printed(0, text),
        Err(error) => ToolAnswer::said(1, format!("clarp-admin transcription: {error}")),
    }
}

/// One agent's part of a change: its value under its session, or, for `default`, nothing
/// under its session and under its persona too where no other agent of this Host is called
/// so (an app may have kept it there), so that nothing of its own is left.
fn entry(app: &App, member: &Member, said: &str) -> Value {
    let record = &member.record;
    let mut one = Map::new();
    if said != DEFAULT {
        one.insert(record.session.clone(), json!(said));
        return Value::Object(one);
    }
    one.insert(record.session.clone(), Value::Null);
    let persona = record.persona.trim().to_lowercase();
    let directory = app.fleet.directory();
    let shared = directory.members.iter().any(|other| {
        other.record.agent_id != record.agent_id
            && other.record.persona.trim().to_lowercase() == persona
    });
    if !persona.is_empty() && !shared {
        one.insert(persona, Value::Null);
    }
    Value::Object(one)
}

/// How the agent is heard now: its language and engines, whether each is its own, and the
/// Host's, which it is heard by where it has none of its own.
fn heard_as(app: &App, member: &Member) -> Value {
    let directory = app.fleet.directory();
    let record = &member.record;
    let who = Who {
        agent_id: &record.agent_id,
        name: &record.persona,
        persona: &record.persona,
        session: &record.session,
    };
    let spoken = languages::languages(directory.setting(languages::AGENT_LANGUAGES_KEY));
    let engines = languages::stored(directory.setting(languages::AGENT_ENGINES_KEY));
    let own_language = languages::of_agent(&spoken, who);
    let own_engines = languages::of_agent(&engines, who).map(languages::engines);
    let host_language = languages::selected(
        directory.setting(languages::LANGUAGE_KEY),
        &app.ears.language,
    );
    json!({
        "session": record.session,
        "agent_id": record.agent_id,
        "language": own_language.map_or(host_language.as_str(), |own| own),
        "own_language": own_language.is_some(),
        "engines": own_engines.clone().unwrap_or_default(),
        "own_engines": own_engines.is_some(),
        "host_language": host_language,
        "host_engine": app.ears.default_engine(&directory),
    })
}
