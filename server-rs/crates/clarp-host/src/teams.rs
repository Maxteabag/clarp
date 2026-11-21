//! The Host's durable team directory, messages, and per-recipient inbox.

mod routes;
pub mod runtime;
pub(crate) use routes::routes;

use std::collections::BTreeMap;
use std::sync::{Mutex, PoisonError};

use clarp_kernel::teams::{Change, Delivery, Team, Teams};
use clarp_ports::kv::{KeyValue, StoreError, Write};
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::{Map, Value, json};

const TEAMS: &str = "team";
const DELIVERIES: &str = "team-message";

pub use clarp_kernel::teams::runtime::Deliveries;

pub struct Desk(Mutex<Stored>);

struct Stored {
    disk: Box<dyn KeyValue>,
    teams: Teams,
    delivery: Deliveries,
    delivery_order: u64,
}

#[derive(Debug)]
pub enum Error {
    Invalid(&'static str),
    Missing(&'static str),
    Storage(StoreError),
}

impl From<StoreError> for Error {
    fn from(error: StoreError) -> Self {
        Self::Storage(error)
    }
}

fn codec_error(error: serde_json::Error) -> StoreError {
    StoreError(error.to_string())
}

fn read<T: DeserializeOwned>(disk: &dyn KeyValue, table: &str) -> Result<Vec<T>, StoreError> {
    disk.list(table)?
        .into_iter()
        .map(|(_, raw)| serde_json::from_slice(&raw).map_err(codec_error))
        .collect()
}

fn write(table: &str, id: &str, row: &impl Serialize) -> Result<Write, StoreError> {
    let value = serde_json::to_vec(row).map_err(codec_error)?;
    Ok((table.into(), id.into(), Some(value)))
}

fn erase(table: &str, id: &str) -> Write {
    (table.into(), id.into(), None)
}

impl Desk {
    pub fn open(disk: Box<dyn KeyValue>) -> Result<Self, StoreError> {
        let teams = read::<Team>(&*disk, TEAMS)?
            .into_iter()
            .map(|team| (team.team_id.clone(), team))
            .collect();
        let mut delivery = Deliveries::new();
        let mut delivery_order = 0;
        for row in read::<Delivery>(&*disk, DELIVERIES)? {
            delivery_order = delivery_order.max(row.order);
            delivery
                .entry(row.message.team_id.clone())
                .or_default()
                .insert(row.message.team_message_id.clone(), row);
        }
        Ok(Self(Mutex::new(Stored {
            disk,
            teams,
            delivery,
            delivery_order,
        })))
    }

    /// Read all the team metadata and receipts from one consistent view.
    pub fn read<R>(&self, inspect: impl FnOnce(&Teams, &Deliveries) -> R) -> R {
        let held = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        inspect(&held.teams, &held.delivery)
    }

    /// Insert new messages and their inbox receipts in a single durable operation.
    /// Already published message IDs are idempotent.
    pub fn deliver(
        &self,
        produce: impl FnOnce(&Teams) -> Vec<Delivery>,
    ) -> Result<usize, StoreError> {
        let mut held = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let mut fresh = BTreeMap::new();
        let mut order = held.delivery_order;
        for mut row in produce(&held.teams) {
            let team = &row.message.team_id;
            let id = &row.message.team_message_id;
            if held
                .teams
                .get(team)
                .is_some_and(|team| team.archived_at.is_none() && team.communication_enabled)
                && !held
                    .delivery
                    .get(team)
                    .is_some_and(|rows| rows.contains_key(id))
            {
                let id = id.clone();
                fresh.entry(id).or_insert_with(|| {
                    order = order.saturating_add(1);
                    row.order = order;
                    row
                });
            }
        }
        let writes = fresh
            .iter()
            .map(|(key, row)| write(DELIVERIES, key, row))
            .collect::<Result<Vec<_>, _>>()?;
        if !writes.is_empty() {
            held.disk.put_together(&writes)?;
        }
        held.delivery_order = order;
        let count = fresh.len();
        for (id, row) in fresh {
            held.delivery
                .entry(row.message.team_id.clone())
                .or_default()
                .insert(id, row);
        }
        Ok(count)
    }

    /// Mark only the inbox items included in the acknowledged prompt.
    pub fn mark_injected(&self, agent: &str, ids: &[String], now: i64) -> Result<(), StoreError> {
        let mut held = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let mut updated = Vec::new();
        for (team_id, rows) in &held.delivery {
            for id in ids {
                if let Some(row) = rows.get(id)
                    && row.inbox.get(agent) == Some(&None)
                {
                    let mut next = row.clone();
                    next.inbox.insert(agent.into(), Some(now));
                    updated.push((team_id.clone(), id.clone(), next));
                }
            }
        }
        let writes = updated
            .iter()
            .map(|(_, id, row)| write(DELIVERIES, id, row))
            .collect::<Result<Vec<_>, _>>()?;
        if !writes.is_empty() {
            held.disk.put_together(&writes)?;
        }
        for (team, id, row) in updated {
            held.delivery.entry(team).or_default().insert(id, row);
        }
        Ok(())
    }

    fn create(&self, id: String, body: &Map<String, Value>, now: i64) -> Result<Value, Error> {
        let mut held = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let team = Team::create(&held.teams, id, body, now).map_err(Error::Invalid)?;
        held.save(&team, false)?;
        Ok(held.payload(&team))
    }

    fn change(&self, id: &str, body: &Map<String, Value>, now: i64) -> Result<Value, Error> {
        let mut held = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let team = held.teams.get(id).ok_or(Error::Missing("team not found"))?;
        let change = Change::read(body).map_err(Error::Invalid)?;
        let next = team
            .update(&held.teams, change, now)
            .map_err(Error::Invalid)?;
        let detach_children = body
            .get("archived")
            .is_some_and(clarp_kernel::python::truthy);
        held.save(&next, detach_children)?;
        Ok(held.payload(&next))
    }

    fn nudge(&self, id: &str, enabled: bool, now: i64) -> Result<Value, Error> {
        let mut held = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let mut team = held
            .teams
            .get(id)
            .cloned()
            .ok_or(Error::Missing("team not found"))?;
        team.updated_at = now;
        team.nudge_enabled = enabled;
        held.save(&team, false)?;
        Ok(held.payload(&team))
    }

    fn add(&self, id: &str, agent: String, now: i64) -> Result<Value, Error> {
        let mut held = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let mut team = held
            .teams
            .get(id)
            .filter(|team| team.archived_at.is_none())
            .cloned()
            .ok_or(Error::Missing("team or agent not found"))?;
        if team.add(agent, now) {
            held.save(&team, false)?;
        }
        Ok(held.payload(&team))
    }

    fn remove(&self, id: &str, agent: &str) -> Result<Value, Error> {
        let mut held = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let Some(mut team) = held.teams.get(id).cloned() else {
            return Ok(Value::Null);
        };
        let mut writes = Vec::new();
        if team.remove(agent) {
            writes.push(write(TEAMS, id, &team)?);
        }
        let mut changed = Vec::new();
        for row in held.delivery.get(id).into_iter().flat_map(BTreeMap::values) {
            if row.inbox.contains_key(agent) {
                let mut next = row.clone();
                next.inbox.remove(agent);
                writes.push(write(DELIVERIES, &next.message.team_message_id, &next)?);
                changed.push(next);
            }
        }
        if !writes.is_empty() {
            held.disk.put_together(&writes)?;
            held.teams.insert(id.into(), team.clone());
            for row in changed {
                held.delivery
                    .entry(id.into())
                    .or_default()
                    .insert(row.message.team_message_id.clone(), row);
            }
        }
        Ok(held.payload(&team))
    }

    fn delete(&self, id: &str) -> Result<(), Error> {
        let mut held = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        if !held.teams.contains_key(id) {
            return Err(Error::Missing("team not found"));
        }
        let children = held.detached_children(id);
        let mut writes = vec![erase(TEAMS, id)];
        for team in &children {
            writes.push(write(TEAMS, &team.team_id, team)?);
        }
        for key in held.delivery.get(id).into_iter().flat_map(BTreeMap::keys) {
            writes.push(erase(DELIVERIES, key));
        }
        held.disk.put_together(&writes)?;
        held.teams.remove(id);
        held.delivery.remove(id);
        for team in children {
            held.teams.insert(team.team_id.clone(), team);
        }
        Ok(())
    }

    fn list(&self) -> Vec<Value> {
        let held = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        let mut rows: Vec<_> = held
            .teams
            .values()
            .filter(|team| team.archived_at.is_none())
            .map(|team| {
                (
                    held.latest(&team.team_id)
                        .map_or(team.updated_at, |row| row.message.created_at),
                    team,
                )
            })
            .collect();
        rows.sort_by(|(at, team), (other_at, other)| {
            other_at
                .cmp(at)
                .then_with(|| team.name.cmp(&other.name))
                .then_with(|| team.team_id.cmp(&other.team_id))
        });
        rows.into_iter()
            .map(|(_, team)| held.payload(team))
            .collect()
    }
}

impl Stored {
    fn detached_children(&self, parent: &str) -> Vec<Team> {
        self.teams
            .values()
            .filter(|team| team.parent_team_id.as_deref() == Some(parent))
            .map(|team| Team {
                parent_team_id: None,
                ..team.clone()
            })
            .collect()
    }

    fn save(&mut self, team: &Team, detach_children: bool) -> Result<(), StoreError> {
        let children = if detach_children {
            self.detached_children(&team.team_id)
        } else {
            Vec::new()
        };
        let mut writes = vec![write(TEAMS, &team.team_id, team)?];
        for child in &children {
            writes.push(write(TEAMS, &child.team_id, child)?);
        }
        self.disk.put_together(&writes)?;
        self.teams.insert(team.team_id.clone(), team.clone());
        for child in children {
            self.teams.insert(child.team_id.clone(), child);
        }
        Ok(())
    }

    fn latest(&self, team: &str) -> Option<&Delivery> {
        self.delivery
            .get(team)?
            .values()
            .max_by_key(|row| (row.message.created_at, &row.message.team_message_id))
    }

    fn payload(&self, team: &Team) -> Value {
        let latest = self.latest(&team.team_id);
        let unread = self
            .delivery
            .get(&team.team_id)
            .into_iter()
            .flat_map(BTreeMap::values)
            .map(|row| {
                row.inbox
                    .values()
                    .filter(|injected| injected.is_none())
                    .count()
            })
            .sum::<usize>();
        json!({
            "team_id": team.team_id,
            "name": team.name,
            "color": team.color,
            "created_at": team.created_at,
            "updated_at": team.updated_at,
            "archived_at": team.archived_at,
            "leader_agent_id": team.leader_agent_id,
            "nudge_enabled": team.nudge_enabled,
            "parent_team_id": team.parent_team_id,
            "leader_enabled": team.leader_enabled,
            "communication_enabled": team.communication_enabled,
            "member_agent_ids": team.members.iter().map(|member| &member.agent_id).collect::<Vec<_>>(),
            "latest_message": latest.map_or("", |row| row.message.text.as_str()),
            "latest_message_at": latest.map(|row| row.message.created_at),
            "unread_count": unread,
        })
    }
}
