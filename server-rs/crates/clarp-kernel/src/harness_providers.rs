//! A harness's model providers: the sign-ins of OpenCode, which can serve models from many
//! providers, as the apps list and change them (the Python Host's `harness_providers.py`).
//!
//! Two harnesses have model providers: OpenCode and DeepSeek, which runs through OpenCode
//! and shows only the providers that serve a DeepSeek model. Both keep their credentials in
//! OpenCode's own store (`auth.json`), and its cache of models.dev (`models.json`) says which
//! providers there are. What is here is pure: the Host reads and writes those files, runs
//! OpenCode's server for a sign-in in a browser, and hands what it read to these rules.
//!
//! A key is never part of an answer: a row carries the type of a credential only.

use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Map, Value, json};

use crate::python;

/// The harness whose store every provider's credential is in.
pub const STORE_HARNESS: &str = "opencode";
/// How long a sign-in in a browser may take before it is given up.
pub const LOGIN_TIMEOUT_SECONDS: i64 = 600;
/// How long what OpenCode said of its sign-in methods is believed before it is asked again.
pub const METHODS_TTL_SECONDS: f64 = 86_400.0;
pub const MAX_KEY_LENGTH: usize = 4096;
pub const MAX_CODE_LENGTH: usize = 512;
/// The setting OpenCode's sign-in methods are remembered in.
pub const METHODS_KEY: &str = "harness.opencode.provider_auth_methods";
/// The providers most people reach for, shown before the long tail of the catalogue.
pub const POPULAR: [&str; 13] = [
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
/// The efforts OpenCode's models may offer, in the order the picker shows them.
const EFFORTS: [&str; 7] = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];

/// What a change is answered with on a Host that does not keep this machine's tools: the
/// tools' logins, settings and stores are one per machine, and the Host beside this one
/// that keeps them is where a provider is signed in or out (the lead's ruling of 20 Nov 2026).
pub const NOT_KEPT: &str = "This Host does not keep this machine's tools; sign in to a model provider on the Host that does.";
pub const UNREADABLE: &str = "OpenCode's credential store could not be read";
pub const UNWRITABLE: &str = "OpenCode's credential store could not be written";
pub const NOT_INSTALLED: &str = "OpenCode is not installed on this Host";
pub const NOT_STARTED: &str = "OpenCode did not start";
pub const NOT_ANSWERED: &str = "OpenCode did not answer the sign-in";
pub const NO_CODE_WAITING: &str = "No sign-in is waiting for a code";
pub const NOT_ASKING_CODE: &str = "This sign-in is not asking for a code";
pub const TIMED_OUT: &str = "Sign-in timed out";
pub const NOT_COMPLETED: &str = "Sign-in was not completed";
pub const CODE_REFUSED: &str = "The code was not accepted";

/// Why a request is refused: a bad request (400) or one that cannot be done now (409).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refused {
    Bad(String),
    Conflict(String),
}

impl Refused {
    pub fn status(&self) -> u16 {
        match self {
            Self::Bad(_) => 400,
            Self::Conflict(_) => 409,
        }
    }

    pub fn words(&self) -> &str {
        match self {
            Self::Bad(words) | Self::Conflict(words) => words,
        }
    }
}

/// A value as Python's `str()` writes it: a text as it is, anything else as it is shown.
fn said(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => python::repr(other),
    }
}

/// `str(value or "")`.
fn said_or_nothing(value: Option<&Value>) -> String {
    value
        .filter(|value| python::truthy(value))
        .map(said)
        .unwrap_or_default()
}

/// Whether a provider's id is one OpenCode's store and the apps can carry.
pub fn valid_id(id: &str) -> bool {
    let first = |sign: u8| sign.is_ascii_lowercase() || sign.is_ascii_digit();
    let rest = |sign: u8| first(sign) || b"._-".contains(&sign);
    let bytes = id.as_bytes();
    (1..=100).contains(&bytes.len())
        && bytes.first().copied().is_some_and(first)
        && bytes[1..].iter().copied().all(rest)
}

/// The harness a request means: nothing means OpenCode. Refused: a harness that has no
/// model providers. A harness that is not a text and not nothing is refused in the same
/// words (the reference ends the connection without an answer).
pub fn harness_id(asked: &Value) -> Result<&'static str, Refused> {
    let fallback = Value::String(STORE_HARNESS.to_string());
    let chosen = if python::truthy(asked) {
        asked
    } else {
        &fallback
    };
    let refused = || Refused::Bad(format!("{} has no model providers", said(chosen)));
    let Value::String(text) = chosen else {
        return Err(refused());
    };
    match python::strip(text).to_lowercase().as_str() {
        "opencode" | "open-code" | "opencode-ai" => Ok("opencode"),
        "deepseek" | "deep-seek" => Ok("deepseek"),
        _ => Err(refused()),
    }
}

/// The provider a request names, in small letters.
pub fn provider_id(asked: &Value) -> Result<String, Refused> {
    let value = python::strip(&said_or_nothing(Some(asked))).to_lowercase();
    if valid_id(&value) {
        Ok(value)
    } else {
        Err(Refused::Bad("provider is required".to_string()))
    }
}

/// A key or a code as it is stored or sent: a text, without the blanks at its ends, not
/// longer than `limit` signs and on one line.
pub fn clean_secret(value: &Value, limit: usize, what: &str) -> Result<String, Refused> {
    let required = || Refused::Bad(format!("{what} is required"));
    let Value::String(text) = value else {
        return Err(required());
    };
    let clean = python::strip(text);
    if clean.is_empty() {
        return Err(required());
    }
    if clean.chars().count() > limit || clean.contains(['\r', '\n', '\0']) {
        return Err(Refused::Bad(format!(
            "{what} is not in the expected format"
        )));
    }
    Ok(clean.to_string())
}

/// OpenCode's catalogue of providers: each that is described. Nothing when there is no file
/// or it is not JSON.
pub fn catalogue(read: Option<&[u8]>) -> Map<String, Value> {
    match read.and_then(|bytes| serde_json::from_slice::<Value>(bytes).ok()) {
        Some(Value::Object(entries)) => {
            entries.into_iter().filter(|(_, v)| v.is_object()).collect()
        }
        _ => Map::new(),
    }
}

/// OpenCode's credential store: `Ok(None)` when there is no file, `Err` when it could not be
/// read. A store that cannot be read is refused, and never echoed: it holds secrets.
pub fn store(read: Result<Option<&[u8]>, ()>) -> Result<Map<String, Value>, Refused> {
    let unreadable = || Refused::Conflict(UNREADABLE.to_string());
    match read {
        Err(()) => Err(unreadable()),
        Ok(None) => Ok(Map::new()),
        Ok(Some(bytes)) => match serde_json::from_slice::<Value>(bytes) {
            Ok(Value::Object(entries)) => Ok(entries),
            Ok(_) => Ok(Map::new()),
            Err(_) => Err(unreadable()),
        },
    }
}

/// Each provider's type of credential (`api`, `oauth`, `wellknown`): nothing secret.
pub fn credential_types(store: &Map<String, Value>) -> Map<String, Value> {
    store
        .iter()
        .filter(|(_, entry)| entry.is_object())
        .map(|(id, entry)| (id.clone(), json!(said_or_nothing(entry.get("type")))))
        .collect()
}

/// The credential types of a store as it is read for a listing: one that cannot be read
/// has none.
pub fn listed_types(read: Result<Option<&[u8]>, ()>) -> Map<String, Value> {
    store(read)
        .map(|store| credential_types(&store))
        .unwrap_or_default()
}

/// What was remembered of OpenCode's sign-in methods.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Remembered {
    pub methods: Map<String, Value>,
    pub version: String,
    /// When, in seconds; 0 when never (or when what was kept says no moment: the reference
    /// fails the listing on that, this Host asks OpenCode again).
    pub at: f64,
}

/// The remembered sign-in methods, from the setting they are kept in.
pub fn remembered(text: Option<&str>) -> Remembered {
    let text = text.filter(|text| !text.is_empty()).unwrap_or("{}");
    let Ok(Value::Object(payload)) = serde_json::from_str::<Value>(text) else {
        return Remembered::default();
    };
    let Some(Value::Object(methods)) = payload.get("methods") else {
        return Remembered::default();
    };
    let at = match payload.get("at").filter(|at| python::truthy(at)) {
        Some(Value::Number(at)) => at.as_f64().unwrap_or(0.0),
        Some(Value::Bool(true)) => 1.0,
        Some(Value::String(at)) => python::strip(at).parse().unwrap_or(0.0),
        _ => 0.0,
    };
    Remembered {
        methods: methods.clone(),
        version: said_or_nothing(payload.get("version")),
        at,
    }
}

/// Whether what was remembered is recent enough not to ask OpenCode again.
pub fn fresh(at: f64, now_seconds: f64) -> bool {
    at != 0.0 && now_seconds - at < METHODS_TTL_SECONDS
}

/// OpenCode's answer of `GET /provider/auth` reduced to what a phone can complete. A method
/// keeps its index in OpenCode's list (the authorize call names it by index). A method that
/// waits for a browser on this machine or for a local CLI cannot be finished from another
/// device and is left out. `None`: the answer is of a shape the reference fails on, and
/// nothing is remembered.
pub fn usable_methods(raw: &Value) -> Option<Map<String, Value>> {
    let mut out = Map::new();
    let Value::Object(raw) = raw else {
        return Some(out);
    };
    for (provider, methods) in raw {
        let Value::Array(methods) = methods else {
            continue;
        };
        let mut kept = Vec::new();
        for (index, method) in methods.iter().enumerate() {
            let Value::Object(method) = method else {
                continue;
            };
            if method.get("type").and_then(Value::as_str) != Some("oauth") {
                continue;
            }
            let label = said_or_nothing(method.get("label"));
            let lowered = label.to_lowercase();
            if lowered.contains("browser") || lowered.contains("azure cli") {
                continue;
            }
            let prompts: Vec<Value> = match method.get("prompts").filter(|p| python::truthy(p)) {
                None | Some(Value::String(_) | Value::Object(_)) => Vec::new(),
                Some(Value::Array(prompts)) => {
                    prompts.iter().filter(|p| p.is_object()).cloned().collect()
                }
                Some(_) => return None,
            };
            kept.push(json!({"index": index, "type": "oauth", "label": label, "prompts": prompts}));
        }
        if !kept.is_empty() {
            out.insert(provider.clone(), Value::Array(kept));
        }
    }
    Some(out)
}

/// What is kept of the sign-in methods once OpenCode was asked.
pub fn remembered_text(version: &str, at: f64, methods: &Map<String, Value>) -> String {
    let at = serde_json::Number::from_f64(at).map_or(Value::Null, Value::Number);
    python::compact(&json!({"version": version, "at": at, "methods": methods}))
}

/// The names of the environment a catalogue entry says hold a key, as the reference reads
/// them (`entry.get("env") or ()`, each as `str()`).
fn env_names(entry: &Value) -> Vec<String> {
    match entry.get("env").filter(|env| python::truthy(env)) {
        Some(Value::Array(names)) => names.iter().map(said).collect(),
        Some(Value::String(names)) => names.chars().map(String::from).collect(),
        Some(Value::Object(names)) => names.keys().cloned().collect(),
        _ => Vec::new(),
    }
}

/// One provider as an app lists it. `env` says whether a variable of the Host's environment
/// is set to something.
pub fn row(
    id: &str,
    entry: &Value,
    types: &Map<String, Value>,
    methods: &Map<String, Value>,
    env: &dyn Fn(&str) -> bool,
) -> Value {
    let names = env_names(entry);
    let credential = match types.get(id) {
        Some(kind) => Some(
            kind.as_str()
                .filter(|k| !k.is_empty())
                .unwrap_or("api")
                .to_string(),
        ),
        None if names.iter().any(|name| env(name)) => Some("env".to_string()),
        None => None,
    };
    let models = entry
        .get("models")
        .and_then(Value::as_object)
        .map_or(0, Map::len);
    let mut sign_in = vec![json!({"index": -1, "type": "api", "label": "API key", "prompts": []})];
    if let Some(Value::Array(offered)) = methods.get(id) {
        sign_in.extend(offered.iter().cloned());
    }
    let name = said_or_nothing(entry.get("name"));
    json!({
        "id": id,
        "name": if name.is_empty() { id.to_string() } else { name },
        "doc": said_or_nothing(entry.get("doc")),
        "env": names,
        "model_count": models,
        "popular": POPULAR.contains(&id),
        "signed_in": credential.is_some(),
        // A key from the Host's environment is not Clarp's to remove.
        "credential": credential.clone(),
        "can_logout": credential.as_deref().is_some_and(|c| c != "env"),
        "methods": sign_in,
    })
}

/// One provider's row, by its id, whether the catalogue lists it or not.
pub fn provider_row(
    id: &str,
    catalogue: &Map<String, Value>,
    types: &Map<String, Value>,
    methods: &Map<String, Value>,
    env: &dyn Fn(&str) -> bool,
) -> Value {
    let named = json!({"name": id});
    let entry = catalogue
        .get(id)
        .filter(|e| python::truthy(e))
        .unwrap_or(&named);
    row(id, entry, types, methods, env)
}

/// A provider's name as the catalogue gives it, else its id.
pub fn provider_name(id: &str, catalogue: &Map<String, Value>) -> String {
    let name = said_or_nothing(catalogue.get(id).and_then(|entry| entry.get("name")));
    if name.is_empty() {
        id.to_string()
    } else {
        name
    }
}

/// Whether the catalogue lists a model of this family for a provider.
fn hosts_family(entry: Option<&Value>, family: &str) -> bool {
    entry
        .and_then(|e| e.get("models"))
        .and_then(Value::as_object)
        .is_some_and(|models| models.keys().any(|id| id.to_lowercase().contains(family)))
}

/// What OpenCode's providers are listed with, as they were read.
pub struct Read<'a> {
    pub catalogue: &'a Map<String, Value>,
    pub types: &'a Map<String, Value>,
    pub methods: &'a Map<String, Value>,
    pub env: &'a dyn Fn(&str) -> bool,
}

/// `GET /harness-providers`: every provider of the catalogue and every one signed in, those
/// signed in first, then the popular ones, then by name.
pub fn listing(harness: &str, read: &Read<'_>, tasks: Value, installed: bool) -> Value {
    let mut rows: Vec<Value> = read
        .catalogue
        .iter()
        .map(|(id, entry)| row(id, entry, read.types, read.methods, read.env))
        .collect();
    // A credential for a provider the catalogue does not list (a custom provider of
    // opencode.json) is still shown so it can be signed out.
    for id in read.types.keys() {
        if !read.catalogue.contains_key(id) && valid_id(id) {
            rows.push(row(
                id,
                &json!({"name": id}),
                read.types,
                read.methods,
                read.env,
            ));
        }
    }
    if harness == "deepseek" {
        rows.retain(|row| {
            let id = row["id"].as_str().unwrap_or_default();
            hosts_family(read.catalogue.get(id), "deepseek") || row["signed_in"] == true
        });
    }
    let rank = |row: &Value| {
        let id = row["id"].as_str().unwrap_or_default();
        (
            row["signed_in"] != true,
            row["popular"] != true,
            POPULAR.iter().position(|p| *p == id).unwrap_or(0),
            row["name"].as_str().unwrap_or_default().to_lowercase(),
        )
    };
    rows.sort_by_cached_key(rank);
    json!({
        "harness": harness,
        "store_harness": STORE_HARNESS,
        "installed": installed,
        "catalogue_available": !read.catalogue.is_empty(),
        "providers": rows,
        "tasks": tasks,
    })
}

/// `GET /harness-providers/models`: a provider's models from the catalogue, as
/// `provider/model` ids, with the efforts each offers. A catalogue entry whose models are not
/// a mapping lists none (the reference ends the connection without an answer).
pub fn models(
    harness: &Value,
    provider: &Value,
    catalogue: &Map<String, Value>,
) -> Result<Value, Refused> {
    let harness = harness_id(harness)?;
    let provider = provider_id(provider)?;
    let Some(entry) = catalogue.get(&provider) else {
        return Err(Refused::Bad(format!("unknown provider {provider}")));
    };
    let family = (harness == "deepseek").then_some("deepseek");
    let empty = Map::new();
    let listed = entry
        .get("models")
        .and_then(Value::as_object)
        .unwrap_or(&empty);
    let mut rows: Vec<Value> = Vec::new();
    for (model, detail) in listed {
        if family.is_some_and(|family| !model.to_lowercase().contains(family)) {
            continue;
        }
        let supported = match detail.get("variants") {
            Some(Value::Object(variants)) => json!(
                EFFORTS
                    .iter()
                    .filter(|e| variants.contains_key(**e))
                    .collect::<Vec<_>>()
            ),
            _ => Value::Null,
        };
        let label = said_or_nothing(detail.get("name"));
        rows.push(json!({
            "id": format!("{provider}/{model}"),
            "label": if label.is_empty() { model.clone() } else { label },
            "model_provider": provider,
            "supported_efforts": supported,
        }));
    }
    rows.sort_by_cached_key(|row| row["label"].as_str().unwrap_or_default().to_lowercase());
    Ok(json!({
        "harness": harness,
        "provider": provider,
        "name": provider_name(&provider, catalogue),
        "models": rows,
    }))
}

/// The code a person types on the provider's page, where OpenCode's instructions name one:
/// four or five letters or digits, a dash, four or five again, standing alone.
pub fn user_code(instructions: &str) -> String {
    let signs: Vec<char> = instructions.chars().collect();
    let class = |at: usize| {
        signs
            .get(at)
            .is_some_and(|s| s.is_ascii_uppercase() || s.is_ascii_digit())
    };
    let run = |from: usize, length: usize| (from..from + length).all(class);
    for start in 0..signs.len() {
        if start > 0 && class(start - 1) {
            continue;
        }
        for first in [5, 4] {
            if !run(start, first) || signs.get(start + first) != Some(&'-') {
                continue;
            }
            let second = start + first + 1;
            for length in [5, 4] {
                if run(second, length) && !class(second + length) {
                    return signs[start..second + length].iter().collect();
                }
            }
        }
    }
    String::new()
}

/// A sign-in's state as the apps poll it.
pub struct Task<'a> {
    pub provider: &'a str,
    pub status: &'a str,
    pub started_at: i64,
    pub method_label: &'a str,
    /// OpenCode's answer to the authorize call: its page, its instructions, how it ends.
    pub authorization: &'a Value,
    pub error: &'a str,
    pub code_submitted: bool,
}

pub fn task(task: &Task<'_>) -> Value {
    let field = |name: &str| said_or_nothing(task.authorization.get(name));
    let instructions = python::first(&field("instructions"), 500).to_string();
    let running = task.status == "running";
    json!({
        "provider": task.provider,
        "status": task.status,
        "method": task.method_label,
        "verification_url": python::first(&field("url"), 2000),
        "instructions": &instructions,
        "user_code": user_code(&instructions),
        "awaiting_code": running && field("method") == "code" && !task.code_submitted,
        "code_submitted": running && task.code_submitted,
        "started_at": task.started_at,
        "expires_at": if running { task.started_at + LOGIN_TIMEOUT_SECONDS * 1000 } else { 0 },
        "error": python::first(task.error, 500),
    })
}

/// OpenCode's answer to the authorize call as a sign-in takes it: an answer of nothing is an
/// answer that tells nothing. `None`: an answer of another shape, which fails the sign-in and
/// ends its server (R156: the reference fails on it and leaves the sign-in running for good,
/// its server never ended; the release owner ruled this answer the rule).
pub fn authorization(answer: &Value) -> Option<Value> {
    if !python::truthy(answer) {
        return Some(json!({}));
    }
    answer.is_object().then(|| answer.clone())
}

/// The task of a provider no sign-in was started for.
pub fn idle(provider: &str) -> Value {
    task(&Task {
        provider,
        status: "idle",
        started_at: 0,
        method_label: "",
        authorization: &Value::Null,
        error: "",
        code_submitted: false,
    })
}

static SECRETS: LazyLock<Regex> = LazyLock::new(|| {
    // Python's `\s` also takes the four separators below the space (`python::blank`).
    let pattern = concat!(
        r#"(?i)(sk-[a-z0-9_-]{8,}|bearer[\s\x1c-\x1f]+[^\s\x1c-\x1f]+"#,
        r#"|"(?:key|access|refresh|token)"[\s\x1c-\x1f]*:[\s\x1c-\x1f]*"[^"]*")"#,
    );
    // Written here: one that does not compile is a mistake in this file.
    #[allow(clippy::expect_used)]
    Regex::new(pattern).expect("a pattern of this module compiles")
});

/// An error of a sign-in as it may be shown: what looks like a key or a token taken out,
/// three hundred signs at most.
pub fn bounded_error(text: &str) -> String {
    python::first(&SECRETS.replace_all(text, "[redacted]"), 300).to_string()
}

/// `POST /harness-providers/login` with a key: the provider and the store with its key.
pub fn save_key(
    harness: &Value,
    provider: &Value,
    key: &Value,
    read: Result<Option<&[u8]>, ()>,
) -> Result<(String, Map<String, Value>), Refused> {
    harness_id(harness)?;
    let provider = provider_id(provider)?;
    let secret = clean_secret(key, MAX_KEY_LENGTH, "API key")?;
    let mut data = store(read)?;
    data.insert(provider.clone(), json!({"type": "api", "key": secret}));
    Ok((provider, data))
}

/// `POST /harness-providers/logout`: the provider and the store without it. Refused: a
/// provider with no credential of OpenCode's, in words that say whether the Host's
/// environment signs it in.
pub fn logout(
    harness: &Value,
    provider: &Value,
    read: Result<Option<&[u8]>, ()>,
    catalogue: &Map<String, Value>,
    methods: &Map<String, Value>,
    env: &dyn Fn(&str) -> bool,
) -> Result<(String, Map<String, Value>), Refused> {
    harness_id(harness)?;
    let provider = provider_id(provider)?;
    let data = store(read)?;
    if !data.contains_key(&provider) {
        let row = provider_row(&provider, catalogue, &credential_types(&data), methods, env);
        let name = row["name"].as_str().unwrap_or(&provider).to_string();
        return Err(Refused::Conflict(if row["credential"] == "env" {
            format!("{name} is signed in through the Host's environment")
        } else {
            format!("{name} is not signed in")
        }));
    }
    let kept = data.into_iter().filter(|(id, _)| *id != provider).collect();
    Ok((provider, kept))
}

/// A sign-in in a browser, as it was asked for and found possible.
#[derive(Debug, Clone, PartialEq)]
pub struct Start {
    pub provider: String,
    pub method: u64,
    pub inputs: Value,
    pub label: String,
}

/// `POST /harness-providers/login` with a method: what is started, or why not.
pub fn start(
    harness: &Value,
    provider: &Value,
    method: &Value,
    inputs: &Value,
    remembered: &Map<String, Value>,
    catalogue: &Map<String, Value>,
    installed: bool,
) -> Result<Start, Refused> {
    harness_id(harness)?;
    let provider = provider_id(provider)?;
    let Some(method) = method.as_u64() else {
        return Err(Refused::Bad(
            "method must be a sign-in method index".to_string(),
        ));
    };
    let inputs = match inputs {
        Value::Null => json!({}),
        Value::Object(fields)
            if fields
                .values()
                .all(|v| v.as_str().is_some_and(|v| v.chars().count() <= 500)) =>
        {
            inputs.clone()
        }
        _ => {
            return Err(Refused::Bad(
                "inputs must map names to short strings".to_string(),
            ));
        }
    };
    let offered = remembered.get(&provider).and_then(Value::as_array);
    let chosen = offered
        .into_iter()
        .flatten()
        .rfind(|m| m["index"].as_u64() == Some(method));
    if !remembered.is_empty() && chosen.is_none() {
        let name = provider_name(&provider, catalogue);
        return Err(Refused::Bad(format!("{name} has no such sign-in method")));
    }
    if !installed {
        return Err(Refused::Conflict(NOT_INSTALLED.to_string()));
    }
    let label = said_or_nothing(chosen.and_then(|m| m.get("label")));
    Ok(Start {
        provider,
        method,
        inputs,
        label,
    })
}

/// `POST /harness-providers/login-code`: the provider and the code, before it is known
/// whether a sign-in waits for one.
pub fn code(harness: &Value, provider: &Value, code: &Value) -> Result<(String, String), Refused> {
    harness_id(harness)?;
    let provider = provider_id(provider)?;
    let code = clean_secret(code, MAX_CODE_LENGTH, "Authorization code")?;
    Ok((provider, code))
}

/// OpenCode's credential store as the reference writes it (`json.dump(indent=2)` and a line's
/// end): two spaces a level, nothing but ASCII.
pub fn store_text(data: &Map<String, Value>) -> String {
    let mut out = String::new();
    indented(&Value::Object(data.clone()), 0, &mut out);
    out.push('\n');
    out
}

fn indented(value: &Value, depth: usize, out: &mut String) {
    let pad = |out: &mut String, depth: usize| out.push_str(&"  ".repeat(depth));
    match value {
        Value::Object(fields) if !fields.is_empty() => {
            out.push('{');
            for (at, (key, value)) in fields.iter().enumerate() {
                out.push_str(if at > 0 { ",\n" } else { "\n" });
                pad(out, depth + 1);
                out.push_str(&python::compact_ascii(&Value::String(key.clone())));
                out.push_str(": ");
                indented(value, depth + 1, out);
            }
            out.push('\n');
            pad(out, depth);
            out.push('}');
        }
        Value::Array(items) if !items.is_empty() => {
            out.push('[');
            for (at, item) in items.iter().enumerate() {
                out.push_str(if at > 0 { ",\n" } else { "\n" });
                pad(out, depth + 1);
                indented(item, depth + 1, out);
            }
            out.push('\n');
            pad(out, depth);
            out.push(']');
        }
        plain => out.push_str(&python::compact_ascii(plain)),
    }
}
