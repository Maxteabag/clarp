//! The owner can change what a chat displays without touching its conversation.

use std::collections::{BTreeMap, HashMap};

use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::{Extension, Json};
use clarp_kernel::hidden_coordination::{self as rules, Visibility};
use clarp_kernel::python;
use serde_json::{Value, json};

use crate::edge::{ApiError, App, Principal};
use crate::fleet::{AgentRecord, Directory};

#[derive(Default)]
pub(crate) struct Roster {
    global: Visibility,
    chats: HashMap<String, Chat>,
}

/// One chat's choices: what holds when it has none of its own, its own, and whether its
/// turns that answer another agent are kept out of it.
#[derive(Clone, Copy)]
struct Chat {
    default: Visibility,
    /// Where the default comes from: `global`, or `helper` for a helper's own chat.
    source: &'static str,
    own: Option<Visibility>,
    hides: bool,
}

/// What a chat shows of the turns other agents start: the visibility that holds when it has
/// no choice of its own, and its own choice. A helper's own chat shows them by default
/// whatever the global choice is: every turn of a helper is started by its parent or another
/// agent, so under a global `hide` its chat would always be empty. The global choice keeps
/// agent chatter out of the chats the owner talks in, and a helper's chat is its work. A chat
/// that was a helper's stays one when the agent's role changes, so nothing it showed is hidden
/// afterwards. A choice the owner made for the chat still wins.
pub(crate) fn chat_choices(
    settings: &BTreeMap<String, String>,
    agent: &AgentRecord,
) -> (Visibility, Option<Visibility>) {
    let read = |key: &str| settings.get(key).and_then(|word| Visibility::read(word));
    let default = if agent.is_helper_chat() {
        Visibility::Show
    } else {
        read(rules::GLOBAL_KEY).unwrap_or_default()
    };
    (default, read(&rules::chat_key(&agent.agent_id)))
}

impl Roster {
    /// The visibility choices that belong to this published directory. The current turn key
    /// can become readable independently, so callers still apply the actor's note capability.
    pub(crate) fn of<'a>(
        settings: &BTreeMap<String, String>,
        agents: impl Iterator<Item = &'a AgentRecord>,
    ) -> Self {
        let global = settings
            .get(rules::GLOBAL_KEY)
            .and_then(|word| Visibility::read(word))
            .unwrap_or_default();
        let chats = agents
            .map(|agent| {
                let (default, own) = chat_choices(settings, agent);
                let hides = own.unwrap_or(default) == Visibility::Hide
                    && clarp_kernel::coordination::can_leave_note(
                        &agent.agent_id,
                        Some(&agent.backend),
                        true,
                    );
                let source = if agent.is_helper_chat() {
                    "helper"
                } else {
                    "global"
                };
                let chat = Chat {
                    default,
                    source,
                    own,
                    hides,
                };
                (agent.agent_id.clone(), chat)
            })
            .collect();
        Self { global, chats }
    }
}

/// What holds for this chat when it has no choice of its own, and its own choice. An agent
/// the directory does not name has the global choice.
pub(crate) fn visibility(
    directory: &Directory,
    agent_id: &str,
) -> (Visibility, Option<Visibility>) {
    let roster = &directory.coordination;
    roster
        .chats
        .get(agent_id)
        .map_or((roster.global, None), |chat| (chat.default, chat.own))
}

/// How a read of this chat's log under `mode` treats coordination: which value holds and
/// where it comes from (the request, the chat's own choice, a helper's own chat, the Host).
pub(crate) fn policy(directory: &Directory, agent_id: &str, mode: rules::Mode) -> rules::Policy {
    let roster = &directory.coordination;
    match roster.chats.get(agent_id) {
        Some(chat) => mode.policy_over((chat.default, chat.source), chat.own),
        None => mode.policy_over((roster.global, "global"), None),
    }
}

pub(crate) fn hides(directory: &Directory, agent_id: &str) -> bool {
    let roster = &directory.coordination;
    roster
        .chats
        .get(agent_id)
        .map_or(roster.global == Visibility::Hide, |chat| chat.hides)
}

fn owner(principal: &Principal, headers: &HeaderMap) -> Result<(), ApiError> {
    if principal.is_agent_request(headers) {
        return Err(ApiError::new(StatusCode::FORBIDDEN, "agents_may_not"));
    }
    Ok(())
}

fn bad(words: &str) -> ApiError {
    ApiError::new(StatusCode::BAD_REQUEST, words)
}

pub(crate) async fn get(State(app): State<App>) -> Json<Value> {
    let (global, _) = visibility(&app.fleet.directory(), "");
    Json(json!({"coordination_visibility": global}))
}

pub(crate) async fn set(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    owner(&principal, &headers)?;
    let asked = serde_json::from_slice::<Value>(&body).unwrap_or_default();
    let asked = asked
        .get("coordination_visibility")
        .ok_or_else(|| bad("coordination_visibility required"))?;
    let value = asked
        .as_str()
        .and_then(Visibility::read)
        .ok_or_else(|| bad(rules::GLOBAL_ERROR))?;
    let effective = app.fleet.set_coordination(None, Some(value)).await?;
    Ok(Json(json!({"coordination_visibility": effective})))
}

pub(crate) async fn set_chat(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    owner(&principal, &headers)?;
    let asked = serde_json::from_slice::<Value>(&body)
        .ok()
        .filter(Value::is_object)
        .ok_or_else(|| bad("bad json"))?;
    let word = &asked["session"];
    let session = if let Some(text) = word.as_str() {
        python::strip(text).to_owned()
    } else if python::truthy(word) {
        python::repr(word)
    } else {
        String::new()
    };
    let raw = asked
        .get("visibility")
        .filter(|_| !session.is_empty())
        .ok_or_else(|| bad("session and visibility required"))?;
    let directory = app.fleet.directory();
    let agent = directory
        .anyone(&session)
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "no such agent"))?;
    let own = rules::chat_choice(raw.as_str().unwrap_or("")).map_err(bad)?;
    let effective = app
        .fleet
        .set_coordination(Some((agent.record.agent_id.clone(), session.clone())), own)
        .await?;
    Ok(Json(json!({
        "ok": true,
        "session": session,
        "coordination_visibility": effective,
        "coordination_override": own,
    })))
}

pub(crate) fn gaps(
    answer: &mut Value,
    page: &clarp_actor::conversation::Page,
    directory: &Directory,
) {
    let public = |run: &clarp_actor::conversation::HiddenRun| {
        let agents: Vec<_> = run
            .senders
            .iter()
            .map(|id| {
                let name = directory
                    .named(id)
                    .map_or("", |member| member.record.persona.as_str());
                json!({"agent_id": id, "name": name})
            })
            .collect();
        json!({"messages": run.messages, "exchanges": run.exchanges,
               "agents": agents, "capped": run.capped})
    };
    if let Some(rows) = answer["turns"].as_array_mut() {
        for row in rows {
            if let Some(run) = row["id"].as_str().and_then(|id| page.hidden_before.get(id)) {
                row["hidden_before"] = public(run);
            }
        }
    }
    if let Some(run) = &page.hidden_after {
        answer["hidden_after"] = public(run);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn published_visibility_changes_with_settings_and_the_roster() {
        let mut agents = [
            AgentRecord {
                agent_id: "ivy".into(),
                backend: "claude".into(),
                ..AgentRecord::default()
            },
            AgentRecord {
                agent_id: "oak".into(),
                backend: "codex".into(),
                ..AgentRecord::default()
            },
            AgentRecord {
                agent_id: "moss".into(),
                backend: "muse".into(),
                ..AgentRecord::default()
            },
        ];
        let mut settings = BTreeMap::from([
            (rules::GLOBAL_KEY.into(), "hide".into()),
            (rules::chat_key("oak"), "show".into()),
        ]);
        let directory = |settings: &BTreeMap<String, String>, agents: &[AgentRecord]| Directory {
            coordination: Roster::of(settings, agents.iter()),
            ..Directory::default()
        };
        let prior = directory(&settings, &agents);
        assert!(hides(&prior, "ivy"));
        assert!(!hides(&prior, "oak"));
        assert!(!hides(&prior, "moss"));
        assert_eq!(
            visibility(&prior, "oak"),
            (Visibility::Hide, Some(Visibility::Show))
        );

        settings.insert(rules::GLOBAL_KEY.into(), "show".into());
        settings.insert(rules::chat_key("ivy"), "hide".into());
        settings.remove(&rules::chat_key("oak"));
        agents[0].backend = "muse".into();
        let current = directory(&settings, &agents);
        assert!(!hides(&current, "ivy"));
        assert_eq!(
            visibility(&current, "ivy"),
            (Visibility::Show, Some(Visibility::Hide))
        );
        assert_eq!(visibility(&current, "oak"), (Visibility::Show, None));
        assert!(hides(&prior, "ivy"));
        assert_eq!(
            visibility(&prior, "oak"),
            (Visibility::Hide, Some(Visibility::Show))
        );
    }

    #[test]
    fn a_helpers_chat_shows_coordination_unless_its_own_choice_hides_it() {
        let helper = AgentRecord {
            agent_id: "hal".into(),
            backend: "claude".into(),
            role: "helper".into(),
            ..AgentRecord::default()
        };
        let plain = AgentRecord {
            agent_id: "oak".into(),
            backend: "claude".into(),
            ..AgentRecord::default()
        };
        let mut settings = BTreeMap::from([(rules::GLOBAL_KEY.into(), "hide".into())]);
        let directory = |settings: &BTreeMap<String, String>| Directory {
            coordination: Roster::of(settings, [&helper, &plain].into_iter()),
            ..Directory::default()
        };
        let shown = directory(&settings);
        assert_eq!(visibility(&shown, "hal"), (Visibility::Show, None));
        assert!(!hides(&shown, "hal"));
        assert_eq!(visibility(&shown, "oak"), (Visibility::Hide, None));
        assert!(hides(&shown, "oak"));
        assert_eq!(chat_choices(&settings, &helper), (Visibility::Show, None));
        let auto = rules::Mode::Auto;
        assert_eq!(
            (
                policy(&shown, "hal", auto).source,
                policy(&shown, "hal", auto).hidden
            ),
            ("helper", false)
        );
        assert_eq!(policy(&shown, "oak", auto).source, "global");

        settings.insert(rules::chat_key("hal"), "hide".into());
        let hidden = directory(&settings);
        assert_eq!(
            visibility(&hidden, "hal"),
            (Visibility::Show, Some(Visibility::Hide))
        );
        assert!(hides(&hidden, "hal"));
        assert_eq!(policy(&hidden, "hal", auto).source, "chat");
    }

    /// Issue 745: what a helper's chat showed stays shown when the agent is a helper no more
    /// (made a janitor, launched again as an agent), also under a global `hide`.
    #[test]
    fn a_chat_that_was_a_helpers_keeps_showing_coordination_when_its_role_changes() {
        let mut hal = AgentRecord {
            agent_id: "hal".into(),
            backend: "claude".into(),
            role: "helper".into(),
            ..AgentRecord::default()
        };
        hal.change_role("janitor".into());
        assert!(!hal.is_helper());
        assert!(hal.is_helper_chat());
        hal.change_role(String::new());
        assert!(hal.is_helper_chat(), "it stays so whatever role comes next");
        let settings = BTreeMap::from([(rules::GLOBAL_KEY.into(), "hide".into())]);
        let shown = Directory {
            coordination: Roster::of(&settings, [&hal].into_iter()),
            ..Directory::default()
        };
        assert_eq!(visibility(&shown, "hal"), (Visibility::Show, None));
        assert!(!hides(&shown, "hal"));
        let auto = rules::Mode::Auto;
        assert_eq!(
            (
                policy(&shown, "hal", auto).source,
                policy(&shown, "hal", auto).hidden
            ),
            ("helper", false)
        );
        // An agent that never was a helper keeps the global choice.
        let mut oak = AgentRecord {
            agent_id: "oak".into(),
            backend: "claude".into(),
            ..AgentRecord::default()
        };
        oak.change_role("janitor".into());
        assert!(!oak.is_helper_chat());
    }
}
