//! Provider transports. Only normalized measurements leave this module. Errors never include
//! headers, subprocess output, request URLs, or the contents of a credential file.
use std::net::SocketAddr;
use std::path::{Path,PathBuf};
use std::process::Stdio;
use std::time::Duration;
use serde_json::{Value,json};
use tokio::io::{AsyncBufReadExt,AsyncReadExt,AsyncWriteExt,BufReader};
use tokio::process::Command;

const LIMIT: u64 = 2*1024*1024;
const DIRECT: Duration = Duration::from_secs(8);

pub(crate) struct Provider { pub loopback: Option<SocketAddr> }
pub(crate) struct Reading {pub value: Value, pub source: &'static str}

fn codex_url(home: &Path) -> Option<String> {
    let base = std::env::var("CODEX_CHATGPT_BASE_URL").ok().filter(|s| !s.is_empty()).or_else(||{
        let file = std::fs::read_to_string(home.join("config.toml")).ok()?;
        let config: toml::Value = toml::from_str(&file).ok()?;
        Some(config.get("chatgpt_base_url")?.as_str()?.trim().to_owned()).filter(|s| !s.is_empty())
    }).unwrap_or_else(|| "https://chatgpt.com/backend-api/".into());
    let uri: axum::http::Uri = base.trim().parse().ok()?;
    let auth = uri.authority()?;
    let host = auth.host().to_ascii_lowercase();
    if uri.scheme_str()!=Some("https") || !matches!(host.as_str(),"chatgpt.com"|"chat.openai.com")
        || ![host.clone(),format!("{host}:443")].contains(&auth.as_str().to_ascii_lowercase())
        || uri.query().is_some() || !matches!(uri.path().trim_end_matches('/'),""|"/backend-api")
        || base.contains('#') {return None;}
    Some(format!("https://{host}/backend-api/wham/usage"))
}

fn http(tool: &str, file: PathBuf, local: Option<SocketAddr>, now: i64) -> Option<Reading> {
    let cred: Value = serde_json::from_slice(&std::fs::read(&file).ok()?).ok()?;
    let claude = tool=="claude";
    let (url, token, account) = if claude {
        let auth = &cred["claudeAiOauth"];
        let expiry = auth["expiresAt"].as_i64().or_else(||auth["expiresAt"].as_str()?.parse().ok()).unwrap_or(0);
        if expiry!=0 && expiry<=now {return None;}
        ("https://api.anthropic.com/api/oauth/usage".into(),auth["accessToken"].as_str()?,None)
    } else {
        if cred["auth_mode"]!="chatgpt" {return None;}
        let auth=&cred["tokens"];
        let account=auth["account_id"].as_str()?.trim();
        if account.is_empty(){return None;}
        (codex_url(file.parent()?)?,auth["access_token"].as_str()?,Some(account))
    };
    let token=token.trim();
    if token.is_empty(){return None;}
    let address = local.map_or(url,|addr|format!("http://{addr}{}",if claude {"/api/oauth/usage"} else {"/backend-api/wham/usage"}));
    let config=ureq::Agent::config_builder().timeout_global(Some(DIRECT)).max_redirects(0).proxy(None).build();
    let agent:ureq::Agent=config.into();
    let mut request=agent.get(address.as_str()).header("Accept","application/json").header("Authorization",&format!("Bearer {token}"));
    request=if let Some(account)=account {request.header("ChatGPT-Account-Id",account).header("User-Agent","codex-cli")} else {request.header("anthropic-beta","oauth-2025-04-20")};
    let mut response=request.call().ok()?;
    if !response.status().is_success(){return None;}
    let body:Value=response.body_mut().with_config().limit(LIMIT).read_json().ok()?;
    if claude {
        Some(Reading{value:clarp_backend::limits::claude_oauth(&body)?,source:"anthropic-oauth-usage"})
    } else {codex_read(&body,"/wham/usage")}
}

fn codex_read(body:&Value,source:&'static str)->Option<Reading>{
    let limits=&body["rate_limit"];
    let blocked=limits["allowed"]==false || limits["limit_reached"]==true;
    let mut value = clarp_backend::limits::codex(body).ok().or_else(||blocked.then(||json!({"windows":{}})))?;
    if blocked {
        let credits=body.pointer("/rate_limit_reached_type/type").and_then(Value::as_str).unwrap_or("").to_ascii_lowercase().contains("credit");
        let reset=body.pointer("/rate_limit_upsell/reset_at").and_then(Value::as_i64).filter(|at| *at>0).and_then(|at|at.checked_mul(1000)).map(clarp_kernel::quota::moment);
        value["blocked"]=json!({"reason":if credits {"credits_depleted"} else {"usage_limit"},"resets_at":reset});
    }
    Some(Reading{value,source})
}

struct Group(i32);
impl Drop for Group {
    fn drop(&mut self) {let _=nix::sys::signal::killpg(nix::unistd::Pid::from_raw(self.0),nix::sys::signal::Signal::SIGKILL);}
}

fn child(tool:&str)->Option<Command>{
    let mut cmd=Command::new(clarp_backend::program(tool)?);
    cmd.process_group(0).kill_on_drop(true).stderr(Stdio::null()).stdout(Stdio::piped());
    for key in crate::OWN_SECRETS {cmd.env_remove(key);}
    Some(cmd)
}

async fn rpc(approval:&str)->Result<Reading,bool>{
    let mut command=child("codex").ok_or(false)?;
    command.args(["-s","read-only"]);
    if !approval.is_empty(){command.args(["-a",approval]);}
    let mut proc=command.arg("app-server").stdin(Stdio::piped()).spawn().map_err(|_|false)?;
    let _group=Group(proc.id().ok_or(false)? as i32);
    let mut input=proc.stdin.take().ok_or(false)?;
    let mut lines=BufReader::new(proc.stdout.take().ok_or(false)?.take(LIMIT)).lines();
    for (id,method,params) in [(1,"initialize",json!({"clientInfo":{"name":"clarp-provider-usage","version":"1"}})),(2,"account/rateLimits/read",json!({}))] {
        let request=json!({"id":id,"method":method,"params":params});
        input.write_all(format!("{request}\n").as_bytes()).await.map_err(|_|false)?;
        while let Some(line)=lines.next_line().await.map_err(|_|false)? {
            let msg:Value=serde_json::from_str(&line).map_err(|_|false)?;
            if msg["id"]!=id {continue}
            if let Some(err)=msg.get("error"){let s=err.to_string().to_ascii_lowercase();return Err(s.contains("approval") || s.contains("invalid value"));}
            if id==2 {return codex_read(&msg["result"],"account/rateLimits/read").ok_or(false);}
            input.write_all(b"{\"method\":\"initialized\",\"params\":{}}\n").await.map_err(|_|false)?;
            break;
        }
    }
    Err(false)
}

struct Temporary(PathBuf);
impl Drop for Temporary {fn drop(&mut self){let _=std::fs::remove_dir_all(&self.0);}}
async fn agy()->Option<Reading>{
    let temp=Temporary(crate::once::a_place("usage").ok()?);
    let given=clarp_backend::by_id("agy")?.own_home()?;
    let home=crate::once::own_home(&temp.0,&given).ok()?;
    let mut proc=child("agy")?.args(["--output-format","json","-p","/usage"]).env("HOME",home)
        .current_dir(temp.0.join("work")).stdin(Stdio::null()).spawn().ok()?;
    let _group=Group(proc.id()? as i32);
    let mut bytes=Vec::new();
    proc.stdout.take()?.take(LIMIT+1).read_to_end(&mut bytes).await.ok()?;
    if bytes.len() as u64>LIMIT || !proc.wait().await.ok()?.success(){return None;}
    let parsed=serde_json::from_slice(&bytes).ok()?;
    Some(Reading{value:clarp_kernel::usage_ask::agy_groups(&parsed)?,source:"agy-usage-command"})
}

impl Provider {
    pub async fn ask(&self,tool:&'static str,home:&Path,now:i64)->Option<Reading>{
        if tool=="agy" {return tokio::time::timeout(Duration::from_secs(20),agy()).await.ok().flatten();}
        let file=clarp_backend::by_id(tool)?.login_file(home)?;
        let local=self.loopback;
        let direct=tokio::task::spawn_blocking(move||http(tool,file,local,now)).await.ok().flatten();
        if direct.is_some() || tool!="codex" {return direct;}
        for approval in ["never","on-request",""]{
            match tokio::time::timeout(DIRECT,rpc(approval)).await {
                Ok(Ok(answer))=>return Some(answer),Ok(Err(true))=>continue,_=>break
            }
        }
        None
    }
}
