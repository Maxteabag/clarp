//! The Host's `/agent-model-options` catalog as the launch dialogs read it.
//! Port of the catalog helpers in the C++ client's `AppController.cpp`.

use serde_json::{Value, json};

use crate::json::{self, Object};

fn providers(catalog: &Object) -> Object {
    json::object(catalog, "providers")
}

fn provider(catalog: &Object, backend: &str) -> Object {
    providers(catalog).get(backend).and_then(Value::as_object).cloned().unwrap_or_default()
}

fn models(catalog: &Object, backend: &str) -> Vec<Object> {
    if let (_, Some(profile)) = split_choice(backend) {
        return provider_profile(catalog, profile).map(|p| p.models).unwrap_or_default();
    }
    json::array(&provider(catalog, backend), "models").into_iter().filter_map(|v| v.as_object().cloned()).collect()
}

// ---- Codex provider profiles (the codex_provider_profiles feature) ------------

/// A launch choice for a Codex provider profile is `codex@<profile>`: the
/// dialogs list it beside the backends, and a create splits it back into
/// `backend` and `provider_profile`.
pub const PROFILE_SEPARATOR: char = '@';

/// One of `providers.codex.provider_profiles`: a Codex agent that runs on
/// another provider ("Codex (EU, Azure)").
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ProviderProfile {
    pub id: String,
    pub label: String,
    /// Why it is (un)available; ends with the operating rule.
    pub detail: String,
    pub operating_rule: String,
    /// False while the Host has no key for it: offered, but disabled.
    pub available: bool,
    pub default_model: String,
    /// `{id, label, supported_efforts}`.
    pub models: Vec<Object>,
}

impl ProviderProfile {
    /// The launch choice id, `codex@<id>`.
    pub fn choice(&self) -> String {
        profile_choice(&self.id)
    }
}

pub fn profile_choice(profile: &str) -> String {
    format!("codex{PROFILE_SEPARATOR}{profile}")
}

/// `codex@azure-eu` -> ("codex", Some("azure-eu")); a plain backend has no
/// profile. Only Codex has profiles.
pub fn split_choice(choice: &str) -> (&str, Option<&str>) {
    match choice.split_once(PROFILE_SEPARATOR) {
        Some(("codex", profile)) if !profile.is_empty() => ("codex", Some(profile)),
        _ => (choice, None),
    }
}

/// The catalog's Codex provider profiles with an id, in the Host's order;
/// none from a Host without the feature.
pub fn provider_profiles(catalog: &Object) -> Vec<ProviderProfile> {
    json::array(&provider(catalog, "codex"), "provider_profiles")
        .iter()
        .filter_map(Value::as_object)
        .filter_map(|p| {
            let id = json::string(p, "id").trim().to_owned();
            if id.is_empty() || id.contains(PROFILE_SEPARATOR) {
                return None;
            }
            let label = json::string(p, "label");
            let models: Vec<Object> = json::array(p, "models")
                .into_iter()
                .filter_map(|m| m.as_object().cloned())
                .filter(|m| !json::string(m, "id").is_empty())
                .collect();
            let default_model = json::string(p, "default_model");
            let default_model = if default_model.is_empty() { models.first().map(|m| json::string(m, "id")).unwrap_or_default() } else { default_model };
            Some(ProviderProfile {
                label: if label.is_empty() { id.clone() } else { label },
                detail: json::string(p, "detail"),
                operating_rule: json::string(p, "operating_rule"),
                available: p.get("available").and_then(Value::as_bool).unwrap_or(false),
                default_model,
                models,
                id,
            })
        })
        .collect()
}

pub fn provider_profile(catalog: &Object, id: &str) -> Option<ProviderProfile> {
    provider_profiles(catalog).into_iter().find(|p| p.id == id)
}

/// [`backend_options`] plus, with `profiles` (the Host advertises contract
/// 63), each Codex provider profile right after Codex as
/// `{id: "codex@<profile>", label, profile, available, detail, operating_rule}`.
pub fn launch_options(catalog: &Object, profiles: bool) -> Vec<Value> {
    let mut options = backend_options(catalog);
    if !profiles {
        return options;
    }
    let Some(codex) = options.iter().position(|o| o["id"] == "codex") else { return options };
    let rows = provider_profiles(catalog).into_iter().map(|p| {
        json!({"id": p.choice(), "label": p.label, "profile": p.id, "available": p.available,
               "detail": p.detail, "operating_rule": p.operating_rule})
    });
    options.splice(codex + 1..codex + 1, rows);
    options
}

fn option(id: &str, label: &str) -> Value {
    json!({"id": id, "label": label})
}

/// Installed, visible providers by `sort_index` (then id); the built-in
/// three when the Host sends none.
pub fn backend_options(catalog: &Object) -> Vec<Value> {
    let mut sorted: Vec<(i64, Value)> = providers(catalog)
        .iter()
        .filter_map(|(id, provider)| {
            let provider = provider.as_object()?;
            let installed = provider.get("installed").and_then(Value::as_bool).unwrap_or(true);
            if json::boolean(provider, "hidden") || !installed {
                return None;
            }
            let index = provider.get("sort_index").and_then(Value::as_i64).unwrap_or(1000);
            let label = provider.get("label").and_then(Value::as_str).unwrap_or(id);
            Some((index, option(id, label)))
        })
        .collect();
    sorted.sort_by_key(|(index, _)| *index);
    let options: Vec<Value> = sorted.into_iter().map(|(_, option)| option).collect();
    if options.is_empty() {
        return vec![option("claude", "Claude"), option("codex", "Codex"), option("agy", "Antigravity")];
    }
    options
}

pub fn models_for_backend(catalog: &Object, backend: &str) -> Vec<Value> {
    let mut result = vec![option("", "Provider default")];
    for model in models(catalog, backend) {
        let id = json::string(&model, "id");
        if !id.is_empty() {
            let label = model.get("label").and_then(Value::as_str).unwrap_or(&id).to_owned();
            result.push(option(&id, &label));
        }
    }
    result
}

pub fn default_effort_for_model(catalog: &Object, backend: &str, model_id: &str) -> String {
    models(catalog, backend)
        .into_iter()
        .find(|m| json::string(m, "id") == model_id)
        .map(|m| json::string(&m, "default_effort"))
        .unwrap_or_default()
}

/// A model's supported efforts, else the provider's, capitalised as labels.
pub fn efforts_for_model(catalog: &Object, backend: &str, model_id: &str) -> Vec<Value> {
    let strings = |values: Vec<Value>| -> Vec<String> {
        values.iter().filter_map(Value::as_str).map(str::to_owned).collect()
    };
    let profile = split_choice(backend).1.and_then(|id| provider_profile(catalog, id));
    let model_id = match &profile {
        Some(p) if model_id.is_empty() => p.default_model.as_str(),
        _ => model_id,
    };
    let mut ids = models(catalog, backend)
        .into_iter()
        .find(|m| json::string(m, "id") == model_id)
        .map(|m| strings(json::array(&m, "supported_efforts")))
        .unwrap_or_default();
    if ids.is_empty() && profile.is_none() {
        ids = strings(json::array(&provider(catalog, backend), "supported_efforts"));
    }
    let mut efforts = vec![option("", "Provider default")];
    for id in ids {
        let mut label = id.clone();
        if let Some(first) = label.get(..1) {
            label = first.to_uppercase() + &label[1..];
        }
        efforts.push(option(&id, &label));
    }
    efforts
}

/// A profile choice supports neither: the Host forks a profile agent only
/// through `POST /agents/<session>/fork`, and resumes only its own
/// conversations.
pub fn backend_supports(catalog: &Object, backend: &str, capability: &str) -> bool {
    if split_choice(backend).1.is_some() {
        return false;
    }
    json::boolean(&provider(catalog, backend), capability)
}

/// Plain words for the Host's provider-profile refusal codes, when a reply
/// carries only the code; None for any other message.
pub fn profile_refusal_text(message: &str) -> Option<&'static str> {
    Some(match message.trim() {
        "provider_profile_boundary" => {
            "Message refused: an agent in the EU data zone can only send to agents in the same zone (provider_profile_boundary)"
        }
        "provider_profile_locked" => {
            "An agent's provider profile and backend are fixed when it is created; start a new agent instead (provider_profile_locked)"
        }
        "provider_profile_mismatch" => "That conversation belongs to another provider profile (provider_profile_mismatch)",
        _ => return None,
    })
}
