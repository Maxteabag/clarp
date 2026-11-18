//! Refresh ages and Antigravity's model-group buckets.
use crate::quota::{NOTHING_SEEN, canonical_id, from_text, moment};
use serde_json::{Value, json};

pub fn due(at: Option<i64>, now: i64) -> bool {
    at.is_none_or(|at| now.saturating_sub(at) > 300_000)
}

/// Only quota fields belong in the store. The provider can include unrelated metadata.
pub fn agy_groups(payload: &Value) -> Option<Value> {
    let groups = payload.pointer("/command/data/groups")?.as_array()?;
    if groups.is_empty() {
        return None;
    }
    let mut kept = Vec::new();
    for group in groups {
        let mut buckets = Vec::new();
        for bucket in group["buckets"].as_array().into_iter().flatten() {
            let fraction = match &bucket["remaining_fraction"] {
                Value::Bool(on) => f64::from(*on as u8),
                value => match value.as_f64() {
                    Some(n) => n,
                    None => continue,
                },
            };
            buckets.push(
                json!({"remaining_fraction":fraction,"window":bucket["window"].as_str(),
                "id":bucket["id"].as_str(), "reset_time":bucket["reset_time"].as_str()}),
            );
        }
        kept.push(json!({"name":group["name"].as_str(),"buckets":buckets}));
    }
    Some(json!(kept))
}

pub fn agy_provider(instance: &str, groups: &Value, at: i64, now: i64) -> Value {
    let mut windows = Vec::new();
    let fresh = now.saturating_sub(at) <= crate::standing::FRESH_MS;
    let age = if fresh { "fresh" } else { "stale" };
    let source = json!({"kind":"provider_reported","detail":"agy-usage-command"});
    for group in groups.as_array().into_iter().flatten() {
        let label = group["name"].as_str().unwrap_or("");
        for bucket in group["buckets"].as_array().into_iter().flatten() {
            let Some(fraction) = bucket["remaining_fraction"].as_f64() else {
                continue;
            };
            let used = format!("{:.1}", (1.0 - fraction) * 100.0)
                .parse::<f64>()
                .ok();
            let reset = bucket["reset_time"].as_str();
            let still = fresh && !reset.and_then(from_text).is_some_and(|end| now >= end);
            let (kind, minutes) = match bucket["window"].as_str() {
                Some("5h") => ("five_hour", Some(300)),
                Some("weekly") => ("seven_day", Some(10080)),
                _ => ("unknown", None),
            };
            let id = canonical_id(
                "puw-",
                &json!({"schema_version":1,"provider_instance_id":instance,
                "auth_generation_id":"agy-usage-command","account_scope_ref":bucket["id"].as_str().filter(|s| !s.is_empty()).unwrap_or(label),
                "scope_kind":"account","window_kind":kind,"unit":"percent","window_minutes":minutes,
                "reset_semantics":reset.filter(|s| !s.is_empty() && used.is_some_and(|v| v>0.0)).unwrap_or("provider_reset_unknown")}),
                32,
            );
            windows.push(json!({"window_id":id,"kind":kind,"scope":"model_group","label":label,"unit":"percent",
                "used_percentage":used,"resets_at":reset,"observed_at":moment(at),"freshness":if still {"fresh"} else {"stale"},
                "source":source,"limit":{"state":if still {"normal"} else {"unknown"},"episode_id":null,"provider_limit_event_id":null}}));
        }
    }
    let any = !windows.is_empty();
    json!({"provider_id":"agy","provider_instance_id":instance,"observed_at":any.then(||moment(at)),
        "freshness":if any {age} else {"unknown"}, "source":if any {source} else {json!({"kind":"unavailable","detail":NOTHING_SEEN})},"windows":windows})
}
