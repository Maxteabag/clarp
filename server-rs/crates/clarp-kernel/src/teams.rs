//! The durable rules for a team's membership, hierarchy, and client view.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::python;
pub mod runtime;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Member {
    pub agent_id: String,
    pub position: usize,
    pub added_at: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Team {
    pub team_id: String,
    pub name: String,
    pub color: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub archived_at: Option<i64>,
    pub leader_agent_id: String,
    pub nudge_enabled: bool,
    pub parent_team_id: Option<String>,
    pub leader_enabled: bool,
    pub communication_enabled: bool,
    pub members: Vec<Member>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Message {
    pub team_message_id: String,
    pub team_id: String,
    pub source_agent_id: String,
    pub source_message_id: String,
    pub trace_id: String,
    pub text: String,
    pub created_at: i64,
}

/// One message and the recipients who were members when it was sent. None is unread.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Delivery {
    /// Break timestamp ties in the order messages reached the team feed.
    #[serde(default)]
    pub order: u64,
    pub message: Message,
    pub inbox: BTreeMap<String, Option<i64>>,
}

#[derive(Debug, Default)]
pub struct Change {
    name: Option<String>,
    color: Option<String>,
    archive: Option<bool>,
    parent: Option<Option<String>>,
    leader: Option<String>,
    leader_enabled: Option<bool>,
    communication_enabled: Option<bool>,
}

/// Python's routes coerce truthy scalar text and treat an empty parent as absent.
pub fn text(value: &Value) -> String {
    if !python::truthy(value) {
        return String::new();
    }
    value
        .as_str()
        .map_or_else(|| python::repr(value), str::to_owned)
}

fn name(raw: &str) -> Result<String, &'static str> {
    let clean = raw
        .split(python::blank)
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    if clean.is_empty() {
        return Err("team name is required");
    }
    Ok(clean)
}

fn parent(value: &Value) -> Result<Option<String>, &'static str> {
    match value {
        Value::Null => Ok(None),
        Value::String(id) => Ok((!id.is_empty()).then(|| id.clone())),
        _ => Err("parent_team_id must be a string or null"),
    }
}

impl Change {
    pub fn read(body: &Map<String, Value>) -> Result<Self, &'static str> {
        let field = |key| body.get(key).filter(|value| !value.is_null());
        let boolean = |key, message| {
            field(key)
                .map(|value| value.as_bool().ok_or(message))
                .transpose()
        };
        Ok(Self {
            name: field("name")
                .map(|value| {
                    name(
                        &value
                            .as_str()
                            .map_or_else(|| python::repr(value), str::to_owned),
                    )
                })
                .transpose()?,
            color: field("color").map(text),
            archive: field("archived").map(python::truthy),
            parent: body.get("parent_team_id").map(parent).transpose()?,
            leader: body
                .get("leader")
                .map(|value| python::strip(&text(value)).to_owned()),
            leader_enabled: boolean("leader_enabled", "leader_enabled must be a boolean")?,
            communication_enabled: boolean(
                "communication_enabled",
                "communication_enabled must be a boolean",
            )?,
        })
    }
}

/// Team metadata keyed by its public id. Durable storage owns serialization of writers.
pub type Teams = BTreeMap<String, Team>;

fn validate_parent(teams: &Teams, team_id: &str, first: Option<&str>) -> Result<(), &'static str> {
    let mut at = first;
    let mut seen = BTreeSet::from([team_id]);
    while let Some(id) = at {
        if !seen.insert(id) {
            return Err("a team cannot contain itself or an ancestor");
        }
        let next = teams
            .get(id)
            .filter(|team| team.archived_at.is_none())
            .ok_or("parent team must exist and be active")?;
        at = next.parent_team_id.as_deref();
    }
    Ok(())
}

impl Team {
    pub fn create(
        teams: &Teams,
        id: String,
        body: &Map<String, Value>,
        now: i64,
    ) -> Result<Self, &'static str> {
        let name = name(&body.get("name").map_or_else(String::new, text))?;
        let parent_team_id = parent(body.get("parent_team_id").unwrap_or(&Value::Null))?;
        validate_parent(teams, &id, parent_team_id.as_deref())?;
        Ok(Self {
            team_id: id,
            name,
            color: body.get("color").map_or_else(String::new, text),
            created_at: now,
            updated_at: now,
            archived_at: None,
            leader_agent_id: String::new(),
            nudge_enabled: false,
            parent_team_id,
            leader_enabled: false,
            communication_enabled: false,
            members: Vec::new(),
        })
    }

    pub fn update(&self, teams: &Teams, change: Change, now: i64) -> Result<Self, &'static str> {
        let mut next = self.clone();
        next.updated_at = now;
        if let Some(name) = change.name {
            next.name = name;
        }
        if let Some(color) = change.color {
            next.color = color;
        }
        if let Some(parent) = change.parent {
            validate_parent(teams, &self.team_id, parent.as_deref())?;
            next.parent_team_id = parent;
        }
        if let Some(archived) = change.archive {
            next.archived_at = archived.then_some(now);
        }
        if let Some(leader) = change.leader {
            if !leader.is_empty() && !self.has(&leader) {
                return Err("leader must be a member of the team");
            }
            next.leader_enabled = !leader.is_empty();
            next.leader_agent_id = leader;
        }
        if let Some(enabled) = change.leader_enabled {
            next.leader_enabled = enabled;
        }
        if let Some(enabled) = change.communication_enabled {
            next.communication_enabled = enabled;
        }
        Ok(next)
    }

    pub fn has(&self, agent: &str) -> bool {
        self.members.iter().any(|member| member.agent_id == agent)
    }

    pub fn add(&mut self, agent: String, now: i64) -> bool {
        if self.has(&agent) {
            return false;
        }
        let position = self.members.last().map_or(0, |member| member.position + 1);
        self.members.push(Member {
            agent_id: agent,
            position,
            added_at: now,
        });
        true
    }

    pub fn remove(&mut self, agent: &str) -> bool {
        let before = self.members.len();
        self.members.retain(|member| member.agent_id != agent);
        let cleared_leader = self.leader_agent_id == agent;
        if cleared_leader {
            self.leader_agent_id.clear();
            self.leader_enabled = false;
        }
        cleared_leader || self.members.len() != before
    }
}

pub fn memberships(teams: &Teams) -> BTreeMap<String, Vec<String>> {
    let mut members = BTreeMap::<String, Vec<(usize, i64, String)>>::new();
    for team in teams.values().filter(|team| team.archived_at.is_none()) {
        for member in &team.members {
            members.entry(member.agent_id.clone()).or_default().push((
                member.position,
                member.added_at,
                team.team_id.clone(),
            ));
        }
    }
    members
        .into_iter()
        .map(|(agent, mut memberships)| {
            memberships.sort_unstable();
            (
                agent,
                memberships.into_iter().map(|(_, _, team)| team).collect(),
            )
        })
        .collect()
}
