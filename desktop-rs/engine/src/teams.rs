//! Teams (the Qt controller's team list, the selected team's messages and
//! the team mutations). Every mutation reloads the list and the selected
//! team's messages when it lands.

use clarp_core::json::{self, Object};
use clarp_core::protocol::display_name;
use serde_json::{Value, json};

use crate::{Change, Engine};

#[derive(Default)]
pub(crate) struct Teams {
    teams: Vec<Value>,
    messages: Vec<Value>,
    selected: String,
    list_loading: bool,
    messages_loading: bool,
    error: String,
    list_generation: u64,
    messages_generation: u64,
}

fn encode(segment: &str) -> String {
    clarp_core::endpoint::percent_encode_segment(segment)
}

impl Engine {
    // ---- queries ---------------------------------------------------------

    pub fn teams(&self) -> &[Value] {
        &self.teams.teams
    }
    /// The selected team's messages, oldest first as the Host sends them.
    pub fn team_messages(&self) -> &[Value] {
        &self.teams.messages
    }
    pub fn selected_team_id(&self) -> &str {
        &self.teams.selected
    }
    pub fn teams_loading(&self) -> bool {
        self.teams.list_loading || self.teams.messages_loading
    }
    pub fn teams_error(&self) -> &str {
        &self.teams.error
    }
    pub fn team(&self, team_id: &str) -> Option<&Object> {
        self.teams.teams.iter().filter_map(Value::as_object).find(|t| t.get("team_id").and_then(Value::as_str) == Some(team_id))
    }
    /// A team's name; its id when unknown or unnamed.
    pub fn team_name_by_id(&self, team_id: &str) -> String {
        self.team(team_id).and_then(|t| t.get("name").and_then(Value::as_str)).unwrap_or(team_id).to_owned()
    }
    /// Agents a team can add: `{id, session, name}` per roster row.
    pub fn team_agent_choices(&self) -> Vec<Value> {
        self.roster.agents().iter().map(|a| json!({"id": a.agent_id, "session": a.session, "name": display_name(a)})).collect()
    }
    /// An agent's name by agent id; the id when it is not in the roster.
    pub fn agent_name_by_id(&self, agent_id: &str) -> String {
        self.roster.find_by_agent_id(agent_id).map_or_else(|| agent_id.to_owned(), |a| display_name(a).to_owned())
    }
    /// An agent's session by agent id; empty when it is not in the roster.
    pub fn agent_session_by_id(&self, agent_id: &str) -> String {
        self.roster.find_by_agent_id(agent_id).map(|a| a.session.clone()).unwrap_or_default()
    }

    // ---- commands --------------------------------------------------------

    pub fn load_teams(&mut self) {
        self.teams.list_generation += 1;
        self.teams.list_loading = true;
        self.teams.error.clear();
        self.changes.push(Change::Teams);
        self.api.get(&format!("team-list:{}", self.teams.list_generation), "/teams", &[]);
    }

    /// Shows a team and (re)loads its messages.
    pub fn select_team(&mut self, team_id: &str) {
        if team_id.is_empty() {
            return;
        }
        let changed = self.teams.selected != team_id;
        self.teams.selected = team_id.to_owned();
        if changed {
            self.teams.messages.clear();
        }
        self.teams.messages_generation += 1;
        self.teams.messages_loading = changed || self.teams.messages.is_empty();
        self.changes.push(Change::Teams);
        let path = format!("/teams/{}/messages", encode(team_id));
        self.api.get(&format!("team-messages:{}:{team_id}", self.teams.messages_generation), &path, &[("limit", "100")]);
    }

    pub fn create_team(&mut self, name: &str, color: &str) {
        let name = name.trim();
        if !name.is_empty() {
            self.api.post_json("team-action:create", "/teams", json!({"name": name, "color": color}), None);
        }
    }

    /// Renames or recolours a team, or names its leader, who must already
    /// be a member.
    pub fn update_team(&mut self, team_id: &str, name: &str, color: &str, leader_agent_id: &str) {
        let name = name.trim();
        if team_id.is_empty() || name.is_empty() {
            return;
        }
        if !leader_agent_id.is_empty() {
            let member = self.team(team_id).map(|t| {
                t.get("member_agent_ids").and_then(Value::as_array).is_some_and(|m| m.iter().any(|a| a.as_str() == Some(leader_agent_id)))
            });
            if member == Some(false) {
                self.teams.error = "Team leader must already be a member".into();
                self.changes.push(Change::Teams);
                return;
            }
        }
        let path = format!("/teams/{}", encode(team_id));
        self.api.post_json("team-action:update", &path, json!({"name": name, "color": color.trim(), "leader": leader_agent_id}), None);
    }

    pub fn add_team_member(&mut self, team_id: &str, agent_id: &str) {
        if team_id.is_empty() || agent_id.is_empty() {
            return;
        }
        let path = format!("/teams/{}/members", encode(team_id));
        self.api.post_json("team-action:add-member", &path, json!({"agent_id": agent_id}), None);
    }

    pub fn remove_team_member(&mut self, team_id: &str, agent_id: &str) {
        if team_id.is_empty() || agent_id.is_empty() {
            return;
        }
        let path = format!("/teams/{}/members/{}", encode(team_id), encode(agent_id));
        self.api.delete("team-action:remove-member", &path);
    }

    pub fn set_team_nudging(&mut self, team_id: &str, enabled: bool) {
        if !team_id.is_empty() {
            self.api.post_json("team-action:nudging", "/team-nudging", json!({"team_id": team_id, "nudge_enabled": enabled}), None);
        }
    }

    pub fn delete_team(&mut self, team_id: &str) {
        if team_id.is_empty() {
            return;
        }
        if self.teams.selected == team_id {
            self.teams.selected.clear();
            self.teams.messages.clear();
            self.changes.push(Change::Teams);
        }
        let path = format!("/teams/{}", encode(team_id));
        self.api.delete("team-action:delete", &path);
    }

    // ---- replies ---------------------------------------------------------

    pub(crate) fn teams_json(&mut self, tag: &str, object: &Object) -> bool {
        if let Some(generation) = tag.strip_prefix("team-list:") {
            if generation.parse::<u64>().ok() != Some(self.teams.list_generation) {
                return true;
            }
            let teams = json::array(object, "teams");
            let selected = self.teams.selected.clone();
            let exists = selected.is_empty() || teams.iter().any(|t| t.get("team_id").and_then(Value::as_str) == Some(selected.as_str()));
            let first = teams.first().and_then(|t| t.get("team_id").and_then(Value::as_str)).map(str::to_owned);
            self.teams.teams = teams;
            self.teams.list_loading = false;
            self.changes.push(Change::Teams);
            // A team that is gone, or none chosen yet: the first one shows.
            if !exists || selected.is_empty() {
                match first {
                    Some(first) => self.select_team(&first),
                    None if !exists => {
                        self.teams.selected.clear();
                        self.teams.messages.clear();
                        self.changes.push(Change::Teams);
                    }
                    None => {}
                }
            }
        } else if let Some(rest) = tag.strip_prefix("team-messages:") {
            let (generation, team) = rest.split_once(':').unwrap_or((rest, ""));
            if generation.parse::<u64>().ok() != Some(self.teams.messages_generation) || team != self.teams.selected {
                return true;
            }
            self.teams.messages = json::array(object, "messages");
            self.teams.messages_loading = false;
            self.changes.push(Change::Teams);
        } else if tag.starts_with("team-action:") {
            let selected = self.teams.selected.clone();
            self.load_teams();
            if !selected.is_empty() {
                self.select_team(&selected);
            }
        } else {
            return false;
        }
        true
    }

    pub(crate) fn teams_failure(&mut self, tag: &str, detail: &str) -> bool {
        let list = tag.starts_with("team-list:");
        let messages = tag.starts_with("team-messages:");
        if !list && !messages && !tag.starts_with("team-action:") {
            return false;
        }
        let generation = tag.split(':').nth(1).and_then(|g| g.parse::<u64>().ok());
        if (list && generation != Some(self.teams.list_generation)) || (messages && generation != Some(self.teams.messages_generation)) {
            return true;
        }
        if list {
            self.teams.list_loading = false;
        } else if messages {
            self.teams.messages_loading = false;
        } else {
            self.teams.list_loading = false;
            self.teams.messages_loading = false;
        }
        self.teams.error = detail.to_owned();
        self.changes.push(Change::Teams);
        true
    }
}
