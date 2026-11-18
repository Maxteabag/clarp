//! The switcher's reading of quota and choice of another machine-wide account.
use crate::python::{casefold, truthy};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Seat {
    pub alias: String,
    pub active: bool,
    pub remaining: Option<i64>,
    pub secondary: Option<i64>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub blocked_models: Vec<String>,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub scoped_remaining: BTreeMap<String, i64>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub degraded: bool,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub overage: bool,
}

fn percent(used: &Value) -> Option<i64> {
    Some(
        (100.0 - used.as_f64()? * 100.0)
            .round_ties_even()
            .clamp(0.0, 100.0) as i64,
    )
}

fn spec(model: &str) -> Option<(String, String)> {
    let model = model.trim();
    let lower = casefold(model);
    for label in ["Fable", "Opus", "Sonnet", "Haiku"] {
        let family = label.to_ascii_lowercase();
        if lower.contains(&family) {
            return Some((family, label.into()));
        }
    }
    let mut bytes = model.bytes();
    if model.len() > 40
        || !bytes.next()?.is_ascii_alphanumeric()
        || !bytes.all(|b| b.is_ascii_alphanumeric() || b"._ -".contains(&b))
    {
        return None;
    }
    Some((lower, model.into()))
}

pub fn parse(tool: &str, body: &Value) -> Vec<Seat> {
    let mut seats = Vec::new();
    for row in body["accounts"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|s| truthy(&s["alias"]))
    {
        let usage = &row["usage"];
        let alias = row["alias"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| row["alias"].to_string());
        let mut seat = Seat {
            alias,
            active: truthy(&row["is_active"]),
            remaining: None,
            secondary: None,
            blocked_models: vec![],
            scoped_remaining: BTreeMap::new(),
            degraded: false,
            overage: false,
        };
        if tool == "codex" {
            if !truthy(&usage["error"]) {
                seat.remaining = if truthy(&usage["blocked"]) {
                    Some(0)
                } else if usage["usable"] == false {
                    None
                } else {
                    percent(&usage["worst_used"])
                };
            }
        } else {
            seat.secondary = percent(&usage["used_7d"]);
            if !truthy(&row["error"]) {
                seat.overage = truthy(&usage["extra_usage_enabled"])
                    || matches!(
                        usage["overage_status"].as_str(),
                        Some("allowed" | "allowed_warning")
                    );
                let full_week = !seat.overage
                    && (seat.secondary == Some(0) || usage["status_7d"] == "rejected");
                seat.remaining = if full_week
                    || truthy(&usage["limited"])
                    || usage["status"] == "rejected"
                    || usage["available"] == false
                {
                    Some(0)
                } else {
                    percent(&usage["used_5h"])
                };
                seat.degraded = truthy(&usage["degraded"]);
                for model in usage["blocked_models"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .take(32)
                {
                    if let Some((family, _)) = model.as_str().and_then(spec)
                        && !seat.blocked_models.contains(&family)
                    {
                        seat.blocked_models.push(family);
                    }
                }
                for scoped in usage["scoped"].as_array().into_iter().flatten().take(32) {
                    let Some((family, _)) = scoped["model"].as_str().and_then(spec) else {
                        continue;
                    };
                    let left = if scoped["exhausted"] == true {
                        Some(0)
                    } else {
                        percent(&scoped["used"])
                    };
                    if let Some(left) = left {
                        if left == 0 && !seat.blocked_models.contains(&family) {
                            seat.blocked_models.push(family.clone());
                        }
                        let n = seat.scoped_remaining.entry(family).or_insert(left);
                        *n = (*n).min(left);
                    }
                }
            }
        }
        seats.push(seat);
    }
    seats
}

impl Seat {
    pub fn serves(&self,model:&str)->bool{
        spec(model).map_or(self.remaining,|(family,_)|self.room(&family)).unwrap_or(0)>0
    }

    pub fn room(&self, family: &str) -> Option<i64> {
        let general = self.remaining?;
        if general <= 0 || self.blocked_models.iter().any(|name| name == family) {
            return Some(0);
        }
        if self.degraded {
            return None;
        }
        Some(
            general.min(
                self.scoped_remaining
                    .get(family)
                    .copied()
                    .unwrap_or(general),
            ),
        )
    }

    fn rank(&self, specs: &[(String, String)]) -> Vec<i64> {
        let mut values = vec![self.remaining.unwrap_or(0)];
        values.extend(
            specs
                .iter()
                .map(|(family, _)| self.room(family).unwrap_or(0)),
        );
        values.sort_unstable();
        values
    }
}

fn specs(models: &[String]) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for (family, label) in models.iter().filter_map(|m| spec(m)) {
        if !out.iter().any(|(f, _)| f == &family) {
            out.push((family, label));
        }
    }
    out
}

fn clears(left: i64, rung: i64) -> bool {
    left >= rung.max(1)
}

pub struct Demand<'a> {
    pub floor: i64,
    pub required: &'a [String],
    pub running: &'a [String],
    pub last: &'a Value,
    pub now: i64,
    pub cooldown: i64,
}

pub fn decide(accounts: &[Seat], ask: Demand<'_>) -> Value {
    let Some(active) = accounts.iter().find(|s| s.active) else {
        return json!({"action":"skip","reason":"Hotseat reports no account in use"});
    };
    let Some(have) = active.remaining else {
        return json!({"action":"skip","reason":format!("{}: quota unknown",active.alias)});
    };
    let req = specs(ask.required);
    let running = specs(ask.running);
    let protected = running
        .iter()
        .chain(&req)
        .filter(|(fam, _)| active.room(fam).unwrap_or(0) > 0)
        .collect::<Vec<_>>();
    let rank = active.rank(&req);
    let since = ask.last["at"]
        .as_f64()
        .map(|at| ask.now.saturating_sub(at as i64));
    let recent = since.is_some_and(|age| (0..ask.cooldown).contains(&age));
    let candidates = accounts
        .iter()
        .filter(|s| {
            if s.active
                || s.remaining.unwrap_or(0) <= 0
                || s.secondary.is_some_and(|v| v <= 0) && !s.overage
            {
                return false;
            }
            if protected
                .iter()
                .any(|(fam, _)| s.room(fam).unwrap_or(0) <= 0)
                || (!req.is_empty() && s.rank(&req) <= rank)
            {
                return false;
            }
            let reversal =
                recent && ask.last["to"] == active.alias && ask.last["from"] == s.alias && have > 0;
            let regained = req
                .iter()
                .any(|(fam, _)| active.room(fam).unwrap_or(0) <= 0 && s.room(fam).unwrap_or(0) > 0);
            !reversal
                || regained
                || (s.remaining.unwrap_or(0) >= ask.floor
                    && req
                        .iter()
                        .all(|(fam, _)| s.room(fam).unwrap_or(0) >= ask.floor))
        })
        .collect::<Vec<_>>();
    let coverage = |s: &Seat| {
        req.iter()
            .filter(|(fam, _)| s.room(fam).unwrap_or(0) > 0)
            .count()
    };
    let complete = candidates.iter().any(|s| coverage(s) == req.len());
    let rungs = [
        Some(ask.floor),
        (10 < ask.floor).then_some(10),
        (0 < ask.floor).then_some(0),
    ];
    let mut noop = None;
    for rung in rungs.into_iter().flatten() {
        if clears(have, rung) {
            noop = Some(rung);
            break;
        }
        let eligible = candidates.iter().copied().filter(|s| {
            clears(s.remaining.unwrap_or(0), rung)
                && (req.is_empty()
                    || if complete {
                        coverage(s) == req.len()
                    } else {
                        coverage(s) > 0
                    })
        });
        let best = eligible.max_by_key(|s| {
            let min = req
                .iter()
                .map(|(fam, _)| s.room(fam).unwrap_or(-1))
                .min()
                .unwrap_or(0);
            (
                clears(min, rung),
                coverage(s),
                s.rank(&req),
                min,
                s.remaining,
                s.secondary.unwrap_or(0),
                &s.alias,
            )
        });
        if let Some(s) = best {
            return json!({"action":"switch","active":active.alias,"remaining":have,"target":s.alias,"target_remaining":s.remaining,"rung":rung});
        }
    }
    let Some(noop_rung) = noop else {
        return json!({"action":"exhausted","active":active.alias,"remaining":have});
    };
    for (family, label) in &req {
        let Some(left) = active.room(family) else {
            continue;
        };
        let mut safe = false;
        for rung in rungs.into_iter().flatten() {
            if clears(left, rung) {
                safe = true;
                break;
            }
            let best = candidates
                .iter()
                .copied()
                .filter(|s| {
                    clears(s.remaining.unwrap_or(0), rung)
                        && clears(s.room(family).unwrap_or(0), rung)
                })
                .max_by_key(|s| {
                    (
                        s.rank(&req),
                        s.room(family),
                        s.remaining,
                        s.secondary.unwrap_or(0),
                        &s.alias,
                    )
                });
            if let Some(s) = best {
                return json!({"action":"switch","active":active.alias,"remaining":left,"target":s.alias,"target_remaining":s.room(family),"target_general_remaining":s.remaining,"rung":rung,"model":label});
            }
        }
        if !safe && left <= 0 {
            if running
                .iter()
                .any(|(f, _)| f != family && active.room(f).unwrap_or(0) > 0)
            {
                return json!({"action":"noop","active":active.alias,"remaining":have,"rung":noop_rung,"blocked_model":label,"model_remaining":0});
            }
            return json!({"action":"exhausted","active":active.alias,"remaining":0,"general_remaining":have,"model":label});
        }
    }
    json!({"action":"noop","active":active.alias,"remaining":have,"rung":noop_rung})
}

/// What a switcher's run shows in the Janitor's history and in an exhaustion notice.
pub fn report(tool:&str,d:&Value,mode:&str,switched:bool)->(String,String){
    let (name,window)=if tool=="claude"{("Claude","5-hour")}else{("Codex","weekly")};
    let active=d["active"].as_str().unwrap_or("?");
    let have=d["remaining"].as_i64().map(|n|n.to_string()).unwrap_or_else(||"?".into());
    let target=d["target"].as_str().unwrap_or("");
    let to=d["target_remaining"].as_i64().unwrap_or(0);
    let model=d["model"].as_str();
    let text=match d["action"].as_str(){
        Some("switch")=>{
            let (target,detail)=if let Some(model)=model{
                let general=d["target_general_remaining"].as_i64().unwrap_or(to);
                (format!("{target} for {model}"),format!("{target} has {to}% {model} quota ({general}% of the {window} window) left; {active} had {have}% {model}"))
            }else{(target.into(),format!("{target} has {to}% of the {window} window left; {active} had {have}%"))};
            let verb=if mode!="automatic" {"would switch to"}else if switched{"switched to"}else{"could not switch to"};
            format!("{name}: {verb} {target}. {detail}")
        }
        Some("exhausted")=>if let Some(model)=model {
            let tail=d["general_remaining"].as_i64().filter(|n|*n!=0).map(|n|format!(" ({active} keeps {n}% of the {window} window for other models)")).unwrap_or_default();
            format!("{name}: {active} has {have}% {model} quota left and no saved account can serve {model}{tail}")
        }else{format!("{name}: {active} has {have}% of the {window} window left and no saved account has more")},
        _=>String::new(),
    };
    let summary=if !text.is_empty(){text.clone()}else if let Some(model)=d["blocked_model"].as_str(){
        let left=d["model_remaining"].as_i64().unwrap_or(0);
        format!("{tool}: {active} has {left}% {model} quota left and no saved account can serve {model}; keeping {active} ({have}% of the {window} window) for running models")
    }else{format!("{tool}: {active} keeps {have}% of the {window} window")};
    (text,summary)
}
