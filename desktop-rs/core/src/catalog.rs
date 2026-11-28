//! The Host's `/agent-model-options` catalog as the launch dialogs read it.
//! Port of the catalog helpers in the C++ client's `AppController.cpp`.

use serde_json::{Value, json};

use crate::json::{self, Object};

pub const LEGACY_ANTIGRAVITY: &str = "agy";
pub const ANTIGRAVITY_ACP: &str = "agy-acp";
pub const ANTIGRAVITY_LABEL: &str = "Antigravity";

pub fn antigravity_label(backend: &str) -> Option<&'static str> {
    matches!(backend, LEGACY_ANTIGRAVITY | ANTIGRAVITY_ACP).then_some(ANTIGRAVITY_LABEL)
}

fn providers(catalog: &Object) -> Object {
    json::object(catalog, "providers")
}

fn provider(catalog: &Object, backend: &str) -> Object {
    providers(catalog).get(backend).and_then(Value::as_object).cloned().unwrap_or_default()
}

fn models(catalog: &Object, backend: &str) -> Vec<Object> {
    json::array(&provider(catalog, backend), "models").into_iter().filter_map(|v| v.as_object().cloned()).collect()
}

fn option(id: &str, label: &str) -> Value {
    json!({"id": id, "label": label})
}

/// Installed, visible launch providers by `sort_index` (then id). Antigravity
/// requires an ACP row the Host offers. Claude and Codex are the offline defaults.
pub fn backend_options(catalog: &Object) -> Vec<Value> {
    let providers = providers(catalog);
    let mut sorted: Vec<_> = providers
        .iter()
        .filter_map(|(id, provider)| {
            let provider = provider.as_object()?;
            let installed = provider.get("installed").and_then(Value::as_bool).unwrap_or(true);
            if json::boolean(provider, "hidden") || !installed {
                return None;
            }
            let index = provider.get("sort_index").and_then(Value::as_i64).unwrap_or(1000);
            Some((index, id, provider))
        })
        .collect();
    if sorted.is_empty() {
        return vec![option("claude", "Claude"), option("codex", "Codex")];
    }
    sorted.sort_by(|(a, id_a, _), (b, id_b, _)| a.cmp(b).then_with(|| id_a.cmp(id_b)));
    sorted
        .into_iter()
        .filter_map(|(_, id, provider)| {
            if id == LEGACY_ANTIGRAVITY || (id == ANTIGRAVITY_ACP && provider.get("available").and_then(Value::as_bool) == Some(false)) {
                return None;
            }
            let label = antigravity_label(id).map_or_else(|| provider.get("label").and_then(Value::as_str).unwrap_or(id), |label| label);
            Some(option(id, label))
        })
        .collect()
}

pub fn models_for_backend(catalog: &Object, backend: &str) -> Vec<Value> {
    let mut result = vec![option("", "Harness default")];
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
    let mut ids = models(catalog, backend)
        .into_iter()
        .find(|m| json::string(m, "id") == model_id)
        .map(|m| strings(json::array(&m, "supported_efforts")))
        .unwrap_or_default();
    if ids.is_empty() {
        ids = strings(json::array(&provider(catalog, backend), "supported_efforts"));
    }
    let mut efforts = vec![option("", "Harness default")];
    for id in ids {
        let mut label = id.clone();
        if let Some(first) = label.get(..1) {
            label = first.to_uppercase() + &label[1..];
        }
        efforts.push(option(&id, &label));
    }
    efforts
}

pub fn backend_supports(catalog: &Object, backend: &str, capability: &str) -> bool {
    json::boolean(&provider(catalog, backend), capability)
}
