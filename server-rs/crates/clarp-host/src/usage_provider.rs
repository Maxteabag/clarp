//! Provider transports. Only normalized measurements leave this module. Errors never include
//! headers, subprocess output, request URLs, or the contents of a credential file.
use clarp_ports::usage::UsageReader;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;

const LIMIT: u64 = 2 * 1024 * 1024;
const DIRECT: Duration = Duration::from_secs(8);

pub(crate) struct Provider {
    pub loopback: Option<SocketAddr>,
}
pub(crate) struct Reading {
    pub value: Value,
    pub source: &'static str,
}

fn codex_url(home: &Path) -> Option<String> {
    let base = std::env::var("CODEX_CHATGPT_BASE_URL")
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| {
            let file = std::fs::read_to_string(home.join("config.toml")).ok()?;
            let config: toml::Value = toml::from_str(&file).ok()?;
            Some(config.get("chatgpt_base_url")?.as_str()?.trim().to_owned())
                .filter(|s| !s.is_empty())
        })
        .unwrap_or_else(|| "https://chatgpt.com/backend-api/".into());
    approved_url(&base)
}

fn approved_url(base: &str) -> Option<String> {
    let uri: axum::http::Uri = base.trim().parse().ok()?;
    let auth = uri.authority()?;
    let host = auth.host().to_ascii_lowercase();
    if uri.scheme_str() != Some("https")
        || !matches!(host.as_str(), "chatgpt.com" | "chat.openai.com")
        || ![host.clone(), format!("{host}:443")].contains(&auth.as_str().to_ascii_lowercase())
        || uri.query().is_some()
        || !matches!(uri.path().trim_end_matches('/'), "" | "/backend-api")
        || base.contains('#')
    {
        return None;
    }
    Some(format!("https://{host}/backend-api/wham/usage"))
}

fn http(
    tool: &str,
    file: PathBuf,
    local: Option<SocketAddr>,
    now: i64,
    stamp: [u8; 32],
) -> Option<Reading> {
    let bytes = std::fs::read(&file).ok()?;
    if <[u8; 32]>::from(Sha256::digest(&bytes)) != stamp {
        return None;
    }
    let cred: Value = serde_json::from_slice(&bytes).ok()?;
    let claude = tool == "claude";
    let (url, token, account) = if claude {
        let auth = &cred["claudeAiOauth"];
        let expiry = match &auth["expiresAt"] {
            v if !clarp_kernel::python::truthy(v) => 0,
            Value::Number(n) => n.as_i64()?,
            Value::String(s) => s.trim().parse().ok()?,
            _ => return None,
        };
        if expiry != 0 && expiry <= now {
            return None;
        }
        (
            "https://api.anthropic.com/api/oauth/usage".into(),
            auth["accessToken"].as_str()?,
            None,
        )
    } else {
        if cred["auth_mode"] != "chatgpt" {
            return None;
        }
        let auth = &cred["tokens"];
        let account = auth["account_id"].as_str()?.trim();
        if account.is_empty() {
            return None;
        }
        (
            codex_url(file.parent()?)?,
            auth["access_token"].as_str()?,
            Some(account),
        )
    };
    let token = token.trim();
    if token.is_empty() {
        return None;
    }
    let address = local.map_or(url, |addr| {
        format!(
            "http://{addr}{}",
            if claude {
                "/api/oauth/usage"
            } else {
                "/backend-api/wham/usage"
            }
        )
    });
    let config = ureq::Agent::config_builder()
        .timeout_global(Some(DIRECT))
        .max_redirects(0)
        .proxy(None)
        .build();
    let agent: ureq::Agent = config.into();
    let mut request = agent
        .get(address.as_str())
        .header("Accept", "application/json")
        .header("Authorization", &format!("Bearer {token}"));
    request = if let Some(account) = account {
        request
            .header("ChatGPT-Account-Id", account)
            .header("User-Agent", "codex-cli")
    } else {
        request.header("anthropic-beta", "oauth-2025-04-20")
    };
    let mut response = request.call().ok()?;
    if !response.status().is_success() {
        return None;
    }
    let body: Value = response
        .body_mut()
        .with_config()
        .limit(LIMIT)
        .read_json()
        .ok()?;
    let reading = if claude {
        Some(Reading {
            value: clarp_backend::limits::claude_oauth(&body)?,
            source: "anthropic-oauth-usage",
        })
    } else {
        codex_read(&body, "/wham/usage")
    }?;
    // A malformed response might echo the authorization into a nominally textual reset.
    (!reading.value.to_string().contains(token)).then_some(reading)
}

fn codex_read(body: &Value, source: &'static str) -> Option<Reading> {
    let limits = &body["rate_limit"];
    let blocked = limits["allowed"] == false || limits["limit_reached"] == true;
    let mut value = clarp_backend::limits::codex(body)
        .ok()
        .or_else(|| blocked.then(|| json!({"windows":{}})))?;
    if blocked {
        let credits = body
            .pointer("/rate_limit_reached_type/type")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_ascii_lowercase()
            .contains("credit");
        let reset = body
            .pointer("/rate_limit_upsell/reset_at")
            .and_then(|value| {
                value
                    .as_f64()
                    .or_else(|| value.as_str()?.parse::<f64>().ok())
            })
            .filter(|at| (1.0..253_402_300_800.0).contains(at))
            .map(|at| (at as i64) * 1000)
            .map(clarp_kernel::quota::moment);
        value["blocked"] = json!({"reason":if credits {"credits_depleted"} else {"usage_limit"},"resets_at":reset});
    }
    Some(Reading { value, source })
}

struct Group(i32);
impl Drop for Group {
    fn drop(&mut self) {
        let _ = nix::sys::signal::killpg(
            nix::unistd::Pid::from_raw(self.0),
            nix::sys::signal::Signal::SIGKILL,
        );
    }
}

fn child(tool: &str) -> Option<Command> {
    let mut cmd = Command::new(clarp_backend::program(tool)?);
    cmd.process_group(0)
        .kill_on_drop(true)
        .stderr(Stdio::null())
        .stdout(Stdio::piped());
    for key in crate::OWN_SECRETS {
        cmd.env_remove(key);
    }
    Some(cmd)
}

fn app_server_read(result: &Value) -> Option<Reading> {
    if !clarp_kernel::python::truthy(&result["rateLimits"])
        && !clarp_kernel::python::truthy(&result["rateLimitsByLimitId"])
    {
        return None;
    }
    codex_read(result, "account/rateLimits/read")
}

async fn rpc(approval: &str) -> Result<Reading, bool> {
    let mut command = child("codex").ok_or(false)?;
    command.args(["-s", "read-only"]);
    if !approval.is_empty() {
        command.args(["-a", approval]);
    }
    let mut proc = command
        .arg("app-server")
        .stdin(Stdio::piped())
        .spawn()
        .map_err(|_| false)?;
    let _group = Group(proc.id().ok_or(false)? as i32);
    let mut input = proc.stdin.take().ok_or(false)?;
    let mut lines = BufReader::new(proc.stdout.take().ok_or(false)?.take(LIMIT)).lines();
    for (id, method, params) in [
        (
            1,
            "initialize",
            json!({"clientInfo":{"name":"clarp-provider-usage","version":"1"}}),
        ),
        (2, "account/rateLimits/read", json!({})),
    ] {
        let request = json!({"id":id,"method":method,"params":params});
        input
            .write_all(format!("{request}\n").as_bytes())
            .await
            .map_err(|_| false)?;
        loop {
            let line = lines.next_line().await.map_err(|_| false)?.ok_or(false)?;
            let msg: Value = serde_json::from_str(&line).map_err(|_| false)?;
            if msg["id"] != id {
                continue;
            }
            if let Some(err) = msg.get("error") {
                let s = err.to_string().to_ascii_lowercase();
                return Err(s.contains("approval") || s.contains("invalid value"));
            }
            if id == 2 {
                return app_server_read(&msg["result"]).ok_or(false);
            }
            input
                .write_all(b"{\"method\":\"initialized\",\"params\":{}}\n")
                .await
                .map_err(|_| false)?;
            break;
        }
    }
    Err(false)
}

struct Temporary(PathBuf);
impl Drop for Temporary {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}
async fn agy() -> Option<Reading> {
    let temp = Temporary(crate::once::a_place("usage").ok()?);
    let given = clarp_backend::by_id("agy")?.own_home()?;
    let home = crate::once::own_home(&temp.0, &given).ok()?;
    let mut proc = child("agy")?
        .args(["--output-format", "json", "-p", "/usage"])
        .env("HOME", home)
        .current_dir(temp.0.join("work"))
        .stdin(Stdio::null())
        .spawn()
        .ok()?;
    let group = Group(proc.id()? as i32);
    let mut bytes = Vec::new();
    let mut stdout = proc.stdout.take()?.take(LIMIT + 1);
    let finish = async {
        stdout.read_to_end(&mut bytes).await.ok()?;
        if bytes.len() as u64 > LIMIT {
            return None;
        }
        proc.wait().await.ok()
    };
    let status = match tokio::time::timeout(Duration::from_secs(20), finish).await {
        Ok(done) => done?,
        Err(_) => {
            // The command may have exited while a descendant still holds stdout. Its
            // answer stands. Ending its process group closes the inherited pipe.
            let done = proc.try_wait().ok()??;
            drop(group);
            tokio::time::timeout(Duration::from_secs(2), stdout.read_to_end(&mut bytes))
                .await
                .ok()?
                .ok()?;
            done
        }
    };
    if !status.success() || bytes.len() as u64 > LIMIT {
        return None;
    }
    let parsed = serde_json::from_slice(&bytes).ok()?;
    Some(Reading {
        value: clarp_kernel::usage_ask::agy_groups(&parsed)?,
        source: "agy-usage-command",
    })
}

struct Https {
    tool: &'static str,
    file: PathBuf,
    loopback: Option<SocketAddr>,
    now_ms: i64,
    stamp: [u8; 32],
}

impl UsageReader for Https {
    type Reading = Reading;
    async fn read(self) -> Option<Reading> {
        tokio::task::spawn_blocking(move || {
            http(self.tool, self.file, self.loopback, self.now_ms, self.stamp)
        })
        .await
        .ok()
        .flatten()
    }
}

struct AppServer;
impl UsageReader for AppServer {
    type Reading = Reading;
    async fn read(self) -> Option<Reading> {
        for approval in ["never", "on-request", ""] {
            match tokio::time::timeout(DIRECT, rpc(approval)).await {
                Ok(Ok(answer)) => return Some(answer),
                Ok(Err(true)) => continue,
                _ => break,
            }
        }
        None
    }
}

struct AgyCommand;
impl UsageReader for AgyCommand {
    type Reading = Reading;
    async fn read(self) -> Option<Reading> {
        agy().await
    }
}

impl Provider {
    pub async fn ask(
        &self,
        tool: &'static str,
        home: &Path,
        now_ms: i64,
        stamp: Option<[u8; 32]>,
    ) -> Option<Reading> {
        if tool == "agy" {
            return AgyCommand.read().await;
        }
        let file = clarp_backend::by_id(tool)?.login_file(home)?;
        let direct = match stamp {
            Some(stamp) => {
                Https {
                    tool,
                    file,
                    loopback: self.loopback,
                    now_ms,
                    stamp,
                }
                .read()
                .await
            }
            None => None,
        };
        if direct.is_some() || tool != "codex" {
            return direct;
        }
        AppServer.read().await
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn provider_readers_agree_with_the_isolated_reference() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../clarp-kernel/tests/fixtures/usage-ask-vectors.json"
        ))
        .unwrap();
        for case in fixture["origins"].as_array().unwrap() {
            assert_eq!(
                json!(approved_url(case["base"].as_str().unwrap())),
                case["url"],
                "{case}"
            );
        }
        for case in fixture["app_server"].as_array().unwrap() {
            let windows = app_server_read(&case["result"]).map(|r| r.value["windows"].clone());
            assert_eq!(json!(windows), case["windows"], "{case}");
        }
        for case in fixture["blocks"].as_array().unwrap() {
            let result =
                codex_read(&case["body"], "/wham/usage").map(|r| r.value["blocked"].clone());
            assert_eq!(json!(result), case["blocked"], "{case}");
        }
        for case in fixture["claude"].as_array().unwrap() {
            assert_eq!(
                json!(clarp_backend::limits::claude_oauth(&case["payload"])),
                case["windows"],
                "{case}"
            );
        }
    }
}
