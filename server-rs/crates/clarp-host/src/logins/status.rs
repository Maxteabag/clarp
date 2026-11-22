//! Account observations for the Host that keeps the tools and is allowed to ask about usage.

use std::path::Path;
use std::time::Duration;

use serde_json::{Value, json};
use tokio::sync::Mutex;

use crate::edge::App;
use crate::usage_provider::status_command;

#[derive(Default)]
pub(super) struct AgyCache {
    last: Option<(i64, Option<[u8; 32]>, AgyStatus)>,
}

#[derive(Clone)]
struct AgyStatus {
    state: &'static str,
    account: String,
    error: &'static str,
}

impl AgyStatus {
    async fn read(home: &Path) -> Self {
        match status_command("agy", &["models"], Duration::from_secs(8)).await {
            Some((true, said))
                if clarp_backend::catalogue::agy_models(&said, "")
                    .iter()
                    .any(|model| model["id"].as_str().is_some_and(|id| id.contains('-'))) =>
            {
                let account = read_json(&home.join(".gemini/google_accounts.json"))
                    .as_ref()
                    .and_then(|data| email(&data["active"]))
                    .unwrap_or_default();
                Self {
                    state: "valid",
                    account,
                    error: "",
                }
            }
            Some((success, _)) => Self {
                state: "signed_out",
                account: String::new(),
                error: if success {
                    ""
                } else {
                    "Antigravity status failed"
                },
            },
            None => Self {
                state: "unverified",
                account: String::new(),
                error: "Antigravity status could not be read",
            },
        }
    }
}

impl AgyCache {
    async fn read(&mut self, app: &App, home: &Path) -> AgyStatus {
        let now = app.clock.now_ms();
        let file = home.join(".gemini/google_accounts.json");
        let stamp = super::digest(&file);
        if let Some((at, seen, status)) = &self.last
            && *seen == stamp
            && (0..60_000).contains(&(now - at))
        {
            return status.clone();
        }
        let status = AgyStatus::read(home).await;
        if super::digest(&file) == stamp {
            self.last = Some((app.clock.now_ms(), stamp, status.clone()));
            status
        } else {
            // A sign-in that changed during observation is looked at again next time.
            AgyStatus {
                state: "unverified",
                account: String::new(),
                error: "",
            }
        }
    }
}

fn read_json(file: &Path) -> Option<Value> {
    serde_json::from_slice(&std::fs::read(file).ok()?).ok()
}

fn email(value: &Value) -> Option<String> {
    let value = value.as_str()?.trim();
    let (name, host) = value.split_once('@')?;
    (!name.is_empty()
        && !host.is_empty()
        && !host.contains('@')
        && value.len() <= 254
        && !value
            .chars()
            .any(|ch| ch.is_whitespace() || ch.is_control()))
    .then(|| value.to_owned())
}

async fn claude_account() -> (String, &'static str) {
    let status = status_command(
        "claude",
        &["auth", "status", "--json"],
        Duration::from_secs(5),
    )
    .await;
    let data = status
        .and_then(|(ok, text)| ok.then_some(text))
        .and_then(|text| serde_json::from_str::<Value>(&text).ok());
    let Some(data) = data.filter(|data| data["loggedIn"] == true) else {
        return (String::new(), "");
    };
    let method = match data["authMethod"].as_str() {
        Some("claude.ai") => "claude.ai",
        Some("oauth") => "oauth",
        Some("api-key") => "api-key",
        _ => "",
    };
    (email(&data["email"]).unwrap_or_default(), method)
}

fn codex_account(file: &Path) -> (String, &'static str) {
    let Some(login) = read_json(file) else {
        return (String::new(), "");
    };
    let account = login["tokens"]["id_token"]
        .as_str()
        .and_then(super::jwt_payload)
        .as_ref()
        .and_then(|claims| email(&claims["email"]))
        .unwrap_or_default();
    let method = if login["auth_mode"] == "chatgpt" {
        "ChatGPT"
    } else {
        ""
    };
    (account, method)
}

pub(super) async fn observe(app: &App, agy: &Mutex<AgyCache>, row: &mut Value) {
    let home = crate::config::home();
    if row["id"] == "agy" {
        let status = agy.lock().await.read(app, &home).await;
        row["state"] = json!(status.state);
        row["logged_in"] = json!(status.state == "valid");
        row["credential_present"] = row["logged_in"].clone();
        row["account"] = json!(status.account);
        row["error"] = json!(status.error);
        return;
    }
    let tool = match row["id"].as_str() {
        Some("claude") => "claude",
        Some("codex") => "codex",
        _ => return,
    };
    if row["state"] != "unverified" {
        return;
    }
    let Some(file) = clarp_backend::by_id(tool).and_then(|backend| backend.login_file(&home))
    else {
        return;
    };
    let Some(stamp) = super::digest(&file) else {
        return;
    };
    if !app.limits.verify_login(app, tool, &home, stamp).await {
        return;
    }
    let Ok(_login) = crate::pool::Service::use_login(app, tool).await else {
        return;
    };
    if super::digest(&file) != Some(stamp) {
        return;
    }
    let (account, method) = match tool {
        "claude" => claude_account().await,
        _ => codex_account(&file),
    };
    if super::digest(&file) == Some(stamp) {
        row["state"] = json!("valid");
        row["logged_in"] = json!(true);
        row["account"] = json!(account);
        row["method"] = json!(method);
    }
}
