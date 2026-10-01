//! The Host's `/agent-model-options` catalog as the launch dialogs read it.
//! Port of the catalog helpers in `desktop/src/app/AppController.cpp`.

use serde_json::{Value, json};

use crate::json::{self, Object};

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
    let mut ids = models(catalog, backend)
        .into_iter()
        .find(|m| json::string(m, "id") == model_id)
        .map(|m| strings(json::array(&m, "supported_efforts")))
        .unwrap_or_default();
    if ids.is_empty() {
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

pub fn backend_supports(catalog: &Object, backend: &str, capability: &str) -> bool {
    json::boolean(&provider(catalog, backend), capability)
}
