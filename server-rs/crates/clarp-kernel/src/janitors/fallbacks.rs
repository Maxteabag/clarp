//! The Host's policy for isolated Janitor model calls.
use super::book::{Agent, Config, World};
use super::{Refused, template};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeSet;

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Policy {
    pub revision: u64,
    pub model_chain: Vec<Model>,
    pub inherit_sessions: Vec<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Model {
    pub provider: String,
    pub model: String,
}

impl Policy {
    pub fn configure(
        &mut self,
        patch: &Value,
        expected: &Value,
        world: &dyn World,
        configs: &std::collections::BTreeMap<String, Config>,
    ) -> Result<Value, Refused> {
        let bad = |message: &str| Refused::new(400, "", message);
        let fields = patch
            .as_object()
            .filter(|f| {
                f.keys()
                    .all(|k| matches!(k.as_str(), "model_chain" | "inherit_sessions"))
            })
            .ok_or_else(|| bad("Unsupported policy fields"))?;
        let janitor = |s: &str| {
            let agent = world.agent(s).filter(|a| a.is_janitor)?;
            let config = configs.get(&agent.agent_id)?;
            Some((agent, config))
        };
        let mut next = self.clone();
        if let Some(inherited) = fields.get("inherit_sessions") {
            let sessions: Vec<String> = serde_json::from_value(inherited.clone())
                .map_err(|_| bad("Expected existing Janitor sessions"))?;
            if sessions.iter().any(|s| janitor(s).is_none()) {
                return Err(bad("Expected existing Janitor sessions"));
            }
            if sessions.iter().collect::<BTreeSet<_>>().len() != sessions.len() {
                return Err(bad("Duplicate inherited session"));
            }
            next.inherit_sessions = sessions;
        }
        if let Some(chain) = fields.get("model_chain") {
            let rows = chain
                .as_array()
                .filter(|r| !r.is_empty())
                .ok_or_else(|| bad("At least one model is required"))?;
            let mut models = Vec::new();
            for row in rows {
                let model: Model = serde_json::from_value(row.clone())
                    .map_err(|_| bad("Each model needs provider and model identifiers"))?;
                if [&model.provider, &model.model]
                    .iter()
                    .any(|s| s.trim().is_empty() || s.chars().count() > 200)
                {
                    return Err(bad("Each model needs provider and model identifiers"));
                }
                models.push(model);
            }
            for model in &models {
                if model.provider == "openai" {
                    continue;
                }
                if model.provider == "scripted" || !world.backend(&model.provider) {
                    return Err(bad("Unsupported model provider"));
                }
                if let Some(error) = world.policy_model_error(&model.provider, &model.model) {
                    return Err(bad(&error));
                }
            }
            if models.iter().collect::<BTreeSet<_>>().len() != models.len() {
                return Err(bad("Duplicate model"));
            }
            next.model_chain = models;
        }
        if expected.as_u64() != Some(self.revision) {
            return Err(Refused::new(
                409,
                "revision_conflict",
                "Policy changed; refresh before saving",
            ));
        }
        for session in &next.inherit_sessions {
            let (agent, config) = janitor(session)
                .filter(|_| !next.model_chain.is_empty())
                .ok_or_else(|| bad("Inherited Janitors require a primary model"))?;
            if local(config) {
                return Err(bad("Deterministic Janitors do not inherit models"));
            }
            let role = template(&config.template_id)?;
            let supports = |m: &Model| {
                role["supported_providers"]
                    .as_array()
                    .map_or(m.provider == agent.backend, |list| {
                        list.iter().any(|p| p == m.provider.as_str())
                    })
            };
            if !next.model_chain.iter().all(supports) {
                return Err(bad(&format!("Model provider incompatible with {session}")));
            }
            if super::turn_templates().contains(&config.template_id)
                && next.model_chain[0].provider != agent.backend
            {
                return Err(bad(
                    "Managed primary provider must match the Janitor backend",
                ));
            }
        }
        next.revision += 1;
        *self = next;
        Ok(json!(self))
    }

    pub fn effective(&self, session: &str, agent: &Agent, config: &Config) -> Value {
        if local(config) {
            return json!({"source":"deterministic","chain":[],"revision":config.revision});
        }
        if self.inherit_sessions.iter().any(|s| s == session) {
            return json!({"source":"global","chain":self.model_chain,"revision":self.revision});
        }
        json!({"source":"explicit","chain":[{"provider":agent.backend,"model":agent.model}],"revision":config.revision})
    }
}
fn local(config: &Config) -> bool {
    config
        .execution
        .get("executor")
        .is_some_and(|v| v == "deterministic")
        || config
            .execution
            .get("provider")
            .is_some_and(|v| v == "local")
}

pub fn retryable(message: &str) -> bool {
    let lower = message.to_lowercase();
    ![
        "permission denied",
        "approval required",
        "not authorized",
        "ownership lost",
        "configuration changed",
    ]
    .iter()
    .any(|deny| lower.contains(deny))
        && crate::failures::classify_error(message) != crate::failures::INTERRUPTED
}

/// Model and effort handed to the provider. Equality includes the effort.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Pick {
    pub backend: String,
    pub model: String,
    pub effort: String,
}

pub fn choices(primary: Pick, effective: &Value) -> (Pick, Vec<(usize, Pick)>) {
    if effective["source"] != "global" {
        return (primary, vec![]);
    }
    let chain = effective["chain"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|row| Pick {
            backend: row["provider"].as_str().unwrap_or_default().into(),
            model: row["model"].as_str().unwrap_or_default().into(),
            effort: String::new(),
        });
    let mut chain = chain.enumerate();
    let Some((_, primary)) = chain.next() else {
        return (primary, vec![]);
    };
    let alternatives = chain
        .filter(|(_, pick)| pick != &primary)
        .map(|(index, pick)| (index - 1, pick))
        .collect();
    (primary, alternatives)
}

pub fn schema_object(text: &str, schema: &Value) -> Option<Value> {
    if text.chars().count() > 512_000 {
        return None;
    }
    for (at, _) in text.match_indices('{').take(64) {
        let parsed = serde_json::Deserializer::from_str(&text[at..])
            .into_iter::<Value>()
            .next();
        if let Some(Ok(value)) = parsed
            && value.is_object()
            && value.get("toolAction").is_none()
            && value.get("toolSummary").is_none()
            && crate::artifacts::answers::first_error(schema, &value).is_none()
        {
            return Some(value);
        }
    }
    None
}
