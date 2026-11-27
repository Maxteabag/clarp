//! The two OpenCode harness cards share a credential store. DeepSeek counts a
//! provider only when its catalogue includes a DeepSeek model.
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};

const POPULAR: &[&str] = &[
    "openrouter",
    "anthropic",
    "openai",
    "deepseek",
    "huggingface",
    "fireworks-ai",
    "groq",
    "github-copilot",
    "xai",
    "google",
    "mistral",
    "togetherai",
    "cerebras",
];

pub(crate) fn store(home: &Path, variable: &str, default: &str, file: &str) -> Map<String, Value> {
    let base = std::env::var(variable)
        .ok()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
        .map_or_else(|| home.join(default), PathBuf::from);
    std::fs::File::open(base.join("opencode").join(file))
        .ok()
        .and_then(|f| serde_json::from_reader(f).ok())
        .unwrap_or_default()
}

fn from_env(entry: &Value) -> bool {
    entry["env"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .any(|var| {
            !var.is_empty()
                && !var.contains(['\0', '='])
                && std::env::var_os(var).is_some_and(|v| !v.is_empty())
        })
}

pub(crate) fn group_models(home: &Path, mut row: Value, deepseek: bool) -> Value {
    let auth = store(home, "XDG_DATA_HOME", ".local/share", "auth.json");
    let catalogue = store(home, "XDG_CACHE_HOME", ".cache", "models.json");
    let mut names = Vec::new();
    for model in row["models"].as_array_mut().into_iter().flatten() {
        if let Some((provider, _)) = model["id"]
            .as_str()
            .and_then(|s| s.split_once('/'))
            .filter(|(p, _)| !p.is_empty())
        {
            let provider = provider.to_owned();
            if !names.contains(&provider) {
                names.push(provider.clone());
            }
            model["model_provider"] = json!(provider);
        }
    }
    if !deepseek {
        for (id, credential) in &auth {
            if credential.is_object() && !names.contains(id) && !id.contains(['/', ':']) {
                names.push(id.clone());
            }
        }
    }
    let providers=names.into_iter().map(|id|{
        let entry=catalogue.get(&id).unwrap_or(&Value::Null);
        let name=entry["name"].as_str().filter(|s|!s.is_empty()).unwrap_or(&id);
        json!({"id":id,"name":name,"signed_in":auth.get(&id).is_some_and(Value::is_object)||id=="opencode"||from_env(entry)})
    }).collect::<Vec<_>>();
    row["harness_providers"] = json!(true);
    row["model_providers"] = json!(providers);
    row
}

fn signed(home: &Path, deepseek: bool) -> Vec<Value> {
    let auth = store(home, "XDG_DATA_HOME", ".local/share", "auth.json");
    let catalogue = store(home, "XDG_CACHE_HOME", ".cache", "models.json");
    let ids: BTreeSet<_> = catalogue.keys().chain(auth.keys()).collect();
    let mut rows = Vec::new();
    for id in ids {
        let entry = catalogue.get(id).filter(|v| v.is_object());
        if entry.is_none() && !clarp_kernel::opencode::valid_provider_id(id) {
            continue;
        }
        let entry = entry.unwrap_or(&Value::Null);
        if deepseek
            && !entry["models"]
                .as_object()
                .is_some_and(|m| m.keys().any(|m| m.to_lowercase().contains("deepseek")))
        {
            continue;
        }
        let credential = auth.get(id).filter(|v| v.is_object()).map(|v| {
            v["type"]
                .as_str()
                .filter(|t| !t.is_empty())
                .unwrap_or("api")
        });
        let Some(kind) = credential.or_else(|| from_env(entry).then_some("env")) else {
            continue;
        };
        let name = entry["name"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or(id);
        let rank = POPULAR
            .iter()
            .position(|p| *p == id)
            .unwrap_or(POPULAR.len());
        rows.push((
            rank,
            name.to_lowercase(),
            json!({"id": id, "name": name, "credential": kind}),
        ));
    }
    rows.sort_by(|a, b| (&a.0, &a.1).cmp(&(&b.0, &b.1)));
    rows.into_iter().map(|(_, _, row)| row).collect()
}

/// Names only. Used by the Accounts usage projection alongside the recorded turns.
pub(crate) fn signed_provider_ids(home: &Path) -> BTreeSet<String> {
    signed(home, false)
        .iter()
        .filter_map(|row| row["id"].as_str())
        .filter(|id| clarp_kernel::opencode::valid_provider_id(id))
        .map(str::to_owned)
        .collect()
}

/// The stock provider's existing Host-wide inference credential. A project can
/// run with another key; the usage reader reports only this key's own cap.
pub(crate) fn openrouter_key(home: &Path) -> Option<String> {
    let auth = store(home, "XDG_DATA_HOME", ".local/share", "auth.json");
    let key = match auth.get("openrouter") {
        Some(saved) if saved["type"] == "api" => saved["key"].as_str()?.to_owned(),
        Some(_) => return None,
        None => std::env::var("OPENROUTER_API_KEY").ok()?,
    };
    let key = key.trim();
    (!key.is_empty()).then(|| key.to_owned())
}

pub(super) fn row(home: &Path, tool: &str, name: &str) -> Value {
    let installed = clarp_backend::program(tool).is_some_and(|program| super::installed(&program));
    let mut row = json!({"id":tool, "name":name, "login_kind":"none", "installed":installed,
        "logged_in":false, "account":"", "method":"", "state":"signed_out", "credential_present":false,
        "can_logout":false, "expires_at":0, "validation_error":"", "error":"",
        "can_login":false, "model_providers":true, "note":""});
    if installed {
        let signed = signed(home, tool == "deepseek");
        let count = signed.len();
        let mut account = signed
            .iter()
            .take(3)
            .filter_map(|v| v["name"].as_str())
            .collect::<Vec<_>>()
            .join(", ");
        if count > 3 {
            account.push_str(&format!(" and {} more", count - 3));
        }
        if count > 0 {
            row["logged_in"] = json!(true);
            row["credential_present"] = json!(true);
            row["state"] = json!("valid");
        }
        row["note"] = json!(match count {
            0 => "Sign in to a model provider to use it.".to_string(),
            1 => "1 model provider signed in.".to_string(),
            n => format!("{n} model providers signed in."),
        });
        row["account"] = json!(account);
        row["model_providers_signed_in"] = json!(signed);
    }
    row
}
