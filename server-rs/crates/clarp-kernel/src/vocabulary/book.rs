//! The vocabulary book is one value. A change consumes a snapshot and only
//! returns the replacement when every part of the request succeeds.
use super::{Pack, Term};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;

pub type Failure = (u16, String);
pub type Result<T> = std::result::Result<T, Failure>;
fn bad(s: &str) -> Failure {
    (400, s.into())
}
fn missing(s: &str) -> Failure {
    (404, s.into())
}
fn conflict(s: &str) -> Failure {
    (409, s.into())
}

#[derive(Default, Clone, Serialize, Deserialize)]
pub struct Book {
    packs: BTreeMap<String, PackEntry>,
    profiles: BTreeMap<String, Profile>,
    assignments: Vec<Assignment>,
    last_term: i64,
}

#[derive(Clone, Serialize, Deserialize)]
struct PackEntry {
    name: String,
    kind: String,
    generator: String,
    priority: f64,
    floor: i64,
    enabled: bool,
    terms: Vec<StoredTerm>,
}

#[derive(Clone, Serialize, Deserialize)]
struct StoredTerm {
    term_id: i64,
    text: String,
    say_as: String,
    often_heard_as: String,
    rarity: f64,
    source: String,
    created_at: i64,
}

#[derive(Clone, Serialize, Deserialize)]
struct Profile {
    name: String,
    harvests_workspace: bool,
    members: BTreeMap<String, i64>,
}
#[derive(Clone, Serialize, Deserialize)]
struct Assignment {
    profile_id: String,
    agent_id: String,
    team_id: String,
    created_at: i64,
}

pub fn text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        v if !crate::python::truthy(v) => String::new(),
        v => crate::python::repr(v),
    }
}
fn required(v: &Value, k: &str) -> Result<String> {
    let val = text(&v[k]);
    if val.is_empty() {
        Err(bad(&format!("{k} is required")))
    } else {
        Ok(val)
    }
}
fn number(v: &Value, default: f64) -> Result<f64> {
    if v.is_null() {
        return Ok(default);
    }
    v.as_f64()
        .or_else(|| v.as_bool().map(|b| u8::from(b) as f64))
        .or_else(|| v.as_str()?.trim().parse().ok())
        .filter(|n: &f64| n.is_finite())
        .ok_or_else(|| bad("a finite number is required"))
}
fn integer(v: &Value, default: i64) -> Result<i64> {
    if v.is_null() {
        return Ok(default);
    }
    v.as_i64()
        .or_else(|| v.as_f64().map(|n| n as i64))
        .or_else(|| v.as_bool().map(i64::from))
        .or_else(|| crate::python::int(v.as_str()?)?.try_into().ok())
        .ok_or_else(|| bad("an integer is required"))
}
fn term_id(v: &Value) -> Result<i64> {
    v.get("term_id")
        .filter(|v| !v.is_null())
        .and_then(|v| integer(v, 0).ok())
        .ok_or_else(|| bad("term_id is required"))
}

impl Book {
    fn add(&mut self, id: &str, v: &Value, default: f64, now: i64) -> Result<bool> {
        let raw = v
            .as_str()
            .map(str::to_string)
            .unwrap_or_else(|| text(&v["text"]));
        let word = raw.trim();
        if word.is_empty() {
            return Ok(false);
        }
        let p = self
            .packs
            .get_mut(id)
            .ok_or_else(|| missing("no such pack"))?;
        let rarity = number(&v["rarity"], default)?;
        // The reference allocates a term id even for a duplicate insertion.
        self.last_term += 1;
        if p.terms.iter().any(|t| t.text.eq_ignore_ascii_case(word)) {
            return Ok(false);
        }
        p.terms.push(StoredTerm {
            term_id: self.last_term,
            text: word.into(),
            say_as: text(&v["say_as"]).trim().into(),
            often_heard_as: text(&v["often_heard_as"]).trim().into(),
            rarity,
            source: v["source"]
                .as_str()
                .filter(|s| !s.is_empty())
                .unwrap_or("manual")
                .into(),
            created_at: now,
        });
        Ok(true)
    }

    pub fn change(
        mut self,
        action: &str,
        v: &Value,
        now: i64,
        suffix: &str,
        known_agent: bool,
    ) -> Result<(Self, Value)> {
        let mut response = json!({"ok":true});
        match action {
            "packs" | "profiles" => {
                let raw = text(&v["name"]);
                let name = raw.trim();
                if name.is_empty() {
                    return Err(bad("name is required"));
                }
                if action == "packs" {
                    if self.packs.values().any(|p| p.name == name) {
                        return Err(conflict("a pack with that name exists"));
                    }
                    let id = format!("pk-{suffix}");
                    if self.packs.contains_key(&id) {
                        return Err(conflict("pack id already exists"));
                    }
                    let kind = text(&v["kind"]);
                    let kind = if kind.is_empty() { "static" } else { &kind };
                    if !matches!(kind, "static" | "dynamic") {
                        return Err(bad("kind must be static or dynamic"));
                    }
                    self.packs.insert(
                        id.clone(),
                        PackEntry {
                            name: name.into(),
                            kind: kind.into(),
                            generator: text(&v["generator"]),
                            priority: number(&v["priority"], 1.0)?,
                            floor: integer(&v["floor"], 0)?,
                            enabled: v.get("enabled").is_none_or(crate::python::truthy),
                            terms: Vec::new(),
                        },
                    );
                    for t in v["terms"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter(|t| t.is_string() || t.is_object())
                    {
                        self.add(&id, t, 0.5, now)?;
                    }
                    response["pack_id"] = json!(id);
                } else {
                    if self.profiles.values().any(|p| p.name == name) {
                        return Err(conflict("a profile with that name exists"));
                    }
                    let id = format!("pf-{suffix}");
                    if self.profiles.contains_key(&id) {
                        return Err(conflict("profile id already exists"));
                    }
                    let mut members = BTreeMap::new();
                    for (pos, pk) in v["pack_ids"].as_array().into_iter().flatten().enumerate() {
                        let pk = text(pk);
                        if !self.packs.contains_key(&pk) {
                            return Err(missing("no such profile or pack"));
                        }
                        members.insert(pk, pos as i64);
                    }
                    self.profiles.insert(
                        id.clone(),
                        Profile {
                            name: name.into(),
                            harvests_workspace: false,
                            members,
                        },
                    );
                    response["profile_id"] = json!(id);
                }
            }
            "packs/update" => {
                let id = required(v, "pack_id")?;
                let raw = text(&v["name"]);
                let name = raw.trim();
                let priority = v
                    .get("priority")
                    .filter(|v| !v.is_null())
                    .map(|v| number(v, 0.0))
                    .transpose()?;
                let floor = v
                    .get("floor")
                    .filter(|v| !v.is_null())
                    .map(|v| integer(v, 0))
                    .transpose()?;
                if self.packs.contains_key(&id)
                    && !name.is_empty()
                    && self.packs.iter().any(|(pk, p)| pk != &id && p.name == name)
                {
                    return Err(conflict("a pack with that name exists"));
                }
                if let Some(p) = self.packs.get_mut(&id) {
                    if !name.is_empty() {
                        p.name = name.into();
                    }
                    if let Some(x) = priority {
                        p.priority = x;
                    }
                    if let Some(x) = floor {
                        p.floor = x;
                    }
                    if let Some(x) = v.get("enabled") {
                        p.enabled = crate::python::truthy(x);
                    }
                }
            }
            "packs/delete" => {
                let pk = required(v, "pack_id")?;
                self.packs.remove(&pk);
                for p in self.profiles.values_mut() {
                    p.members.remove(&pk);
                }
            }
            "profiles/delete" => {
                let pf = required(v, "profile_id")?;
                self.profiles.remove(&pf);
                self.assignments.retain(|a| a.profile_id != pf);
            }
            "terms" => {
                let pk = text(&v["pack_id"]);
                let t = text(&v["text"]);
                if pk.is_empty() || t.trim().is_empty() {
                    return Err(bad("pack_id and text are required"));
                }
                response["added"] = json!(self.add(&pk, v, super::rarity(&t), now)?);
            }
            "terms/update" => {
                let id = term_id(v)?;
                let raw = text(&v["text"]);
                let name = raw.trim();
                let field = |k: &str| {
                    v.get(k)
                        .filter(|v| !v.is_null())
                        .map(|v| text(v).trim().to_string())
                };
                let rarity = v
                    .get("rarity")
                    .filter(|v| !v.is_null())
                    .map(|v| number(v, 0.0).map(|n| n.clamp(0.0, 1.0)))
                    .transpose()?;
                let mut updated = false;
                for p in self.packs.values_mut() {
                    let Some(at) = p.terms.iter().position(|t| t.term_id == id) else {
                        continue;
                    };
                    if !name.is_empty()
                        && p.terms
                            .iter()
                            .any(|t| t.term_id != id && t.text.eq_ignore_ascii_case(name))
                    {
                        return Err(conflict("that term already exists in the pack"));
                    }
                    let t = &mut p.terms[at];
                    if !name.is_empty() {
                        t.text = name.into();
                        updated = true;
                    }
                    if let Some(x) = field("say_as") {
                        t.say_as = x;
                        updated = true;
                    }
                    if let Some(x) = field("often_heard_as") {
                        t.often_heard_as = x;
                        updated = true;
                    }
                    if let Some(x) = rarity {
                        t.rarity = x;
                        updated = true;
                    }
                    break;
                }
                response["updated"] = json!(updated);
            }
            "terms/delete" => {
                let id = term_id(v)?;
                for p in self.packs.values_mut() {
                    p.terms.retain(|t| t.term_id != id);
                }
            }
            "profiles/packs" | "profiles/packs/remove" => {
                let (pf, pk) = (text(&v["profile_id"]), text(&v["pack_id"]));
                if pf.is_empty() || pk.is_empty() {
                    return Err(bad("profile_id and pack_id are required"));
                }
                if action.ends_with("/remove") {
                    if let Some(p) = self.profiles.get_mut(&pf) {
                        p.members.remove(&pk);
                    }
                } else {
                    let pos = integer(&v["position"], 0)?;
                    let p = self
                        .profiles
                        .get_mut(&pf)
                        .filter(|_| self.packs.contains_key(&pk))
                        .ok_or_else(|| missing("no such profile or pack"))?;
                    p.members.insert(pk, pos);
                }
            }
            "profiles/workspace" => {
                let pf = required(v, "profile_id")?;
                let pf = pf.trim();
                let enabled = v["enabled"]
                    .as_bool()
                    .ok_or_else(|| bad("enabled must be a boolean"))?;
                self.profiles
                    .get_mut(pf)
                    .ok_or_else(|| missing("no such profile"))?
                    .harvests_workspace = enabled;
                response = json!({"ok":true,"profile_id":pf,"harvests_workspace":enabled});
            }
            "assign" | "unassign" => {
                let a = text(&v["agent_id"]);
                let t = text(&v["team_id"]);
                let (a, t) = (a.trim(), t.trim());
                if action == "assign" {
                    let pf = required(v, "profile_id")?;
                    if a.is_empty() == t.is_empty() {
                        return Err(bad("assign to exactly one of agent_id or team_id"));
                    }
                    if !self.profiles.contains_key(&pf) || (!a.is_empty() && !known_agent) {
                        return Err(missing("no such profile"));
                    }
                    self.unassign(a, t);
                    self.assignments.push(Assignment {
                        profile_id: pf,
                        agent_id: a.into(),
                        team_id: t.into(),
                        created_at: now,
                    });
                } else {
                    if a.is_empty() && t.is_empty() {
                        return Err(bad("agent_id or team_id is required"));
                    }
                    self.unassign(a, t);
                }
            }
            _ => return Err(missing("not found")),
        }
        Ok((self, response))
    }

    fn unassign(&mut self, agent: &str, team: &str) {
        self.assignments.retain(|a| {
            (agent.is_empty() || a.agent_id != agent) && (team.is_empty() || a.team_id != team)
        });
    }

    fn members<'a>(&'a self, p: &'a Profile) -> Vec<(&'a String, &'a PackEntry, i64)> {
        let mut rows: Vec<_> = p
            .members
            .iter()
            .filter_map(|(id, pos)| Some((id, self.packs.get(id)?, *pos)))
            .collect();
        rows.sort_by(|a, b| a.2.cmp(&b.2).then_with(|| a.1.name.cmp(&b.1.name)));
        rows
    }

    pub fn read(&self, action: &str, v: &Value) -> Result<Value> {
        Ok(match action {
            "packs" => {
                let mut rows: Vec<_> = self.packs.iter().collect();
                rows.sort_by_key(|(_, p)| &p.name);
                json!({"packs":rows.into_iter().map(|(id,p)|json!({"pack_id":id,"name":p.name,"kind":p.kind,"generator":p.generator,"priority":p.priority,"floor":p.floor,"enabled":p.enabled,"terms":p.terms.len()})).collect::<Vec<_>>()})
            }
            "terms" => {
                let id = required(v, "pack_id")?;
                let mut terms: Vec<_> = self
                    .packs
                    .get(&id)
                    .into_iter()
                    .flat_map(|p| &p.terms)
                    .collect();
                terms.sort_by(|a, b| {
                    b.rarity.total_cmp(&a.rarity).then_with(|| {
                        a.text
                            .to_ascii_lowercase()
                            .cmp(&b.text.to_ascii_lowercase())
                    })
                });
                json!({"pack_id":id,"terms":terms})
            }
            "profiles" => {
                let mut rows: Vec<_> = self.profiles.iter().collect();
                rows.sort_by_key(|(_, p)| &p.name);
                json!({"profiles":rows.into_iter().map(|(id,p)|json!({"profile_id":id,"name":p.name,"packs":p.members.len(),"assignments":self.assignments.iter().filter(|a|a.profile_id==*id).count()})).collect::<Vec<_>>()})
            }
            "profile" => {
                let id = text(&v["profile_id"]);
                let p = self
                    .profiles
                    .get(&id)
                    .ok_or_else(|| missing("no such profile"))?;
                let members=self.members(p).into_iter().map(|(pk,pack,pos)|json!({"pack_id":pk,"position":pos,"name":pack.name,"kind":pack.kind,"enabled":pack.enabled})).collect::<Vec<_>>();
                let assignments = self
                    .assignments
                    .iter()
                    .filter(|a| a.profile_id == id)
                    .map(|a| json!({"agent_id":a.agent_id,"team_id":a.team_id}))
                    .collect::<Vec<_>>();
                json!({"profile_id":id,"name":p.name,"packs":members,"assignments":assignments,"harvests_workspace":p.harvests_workspace})
            }
            "assignments" => {
                let mut rows: Vec<_> = self.assignments.iter().collect();
                rows.sort_by_key(|a| a.created_at);
                json!({"assignments":rows.into_iter().map(|a|json!({"profile_id":a.profile_id,"profile_name":self.profiles[&a.profile_id].name,"agent_id":a.agent_id,"team_id":a.team_id})).collect::<Vec<_>>()})
            }
            _ => return Err(missing("not found")),
        })
    }

    pub fn assigned(&self, agent: &str) -> Option<(&str, bool, Vec<Pack>)> {
        let id = &self
            .assignments
            .iter()
            .find(|a| !agent.is_empty() && a.agent_id == agent)?
            .profile_id;
        let p = self.profiles.get(id)?;
        let packs = self
            .members(p)
            .into_iter()
            .map(|(_, p, _)| {
                let mut terms: Vec<_> = p
                    .terms
                    .iter()
                    .filter(|_| p.kind == "static")
                    .map(|t| Term {
                        text: t.text.clone(),
                        pack: p.name.clone(),
                        rarity: t.rarity,
                        recency: 0.0,
                        confusable: !t.often_heard_as.trim().is_empty(),
                        say_as: Some(t.say_as.clone()).filter(|s| !s.is_empty()),
                    })
                    .collect();
                terms.sort_by(|a, b| b.rarity.total_cmp(&a.rarity));
                Pack {
                    name: p.name.clone(),
                    enabled: p.enabled,
                    floor: p.floor,
                    priority: p.priority,
                    terms,
                }
            })
            .collect();
        Some((id, p.harvests_workspace, packs))
    }

    pub fn corrections(&self) -> Vec<(String, String)> {
        let mut terms: Vec<_> = self
            .packs
            .values()
            .filter(|p| p.enabled)
            .flat_map(|p| &p.terms)
            .filter(|t| !t.often_heard_as.is_empty())
            .collect();
        terms.sort_by_key(|t| (std::cmp::Reverse(t.created_at), t.term_id));
        terms
            .into_iter()
            .take(200)
            .flat_map(|t| {
                t.often_heard_as
                    .split([',', ';'])
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .map(|s| (s.into(), t.text.clone()))
            })
            .collect()
    }
}
