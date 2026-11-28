//! The Hotseat switcher's banked-reset lane: when no account of a tool has room, the owner's
//! reset tool is asked to spend one banked reset, under the owner's rule
//! ([`clarp_kernel::banked_reset`]): every account out, and the soonest natural reset among
//! them more than 48 hours away. The Python Host's lane (`janitor_reset.py`), rebuilt.
//!
//! The tool is the owner's (`[agents] claude_reset_command`, `codex_reset_command` in this
//! Host's config, an argv list; empty is off). It is started with one JSON request on stdin
//! and nothing of the Host's own environment but its home, path, locale and XDG directories,
//! and has [`banked_reset::TIMEOUT_SECONDS`]. A verified redemption switches to the refilled
//! account the way the switcher does, and the owner and the sessions of `[agents] reset_tell`
//! are told; so they are, once, of a spend that could not be verified and of a reconciliation
//! that gave up (then the lane spends nothing until someone looks).

use super::*;
use clarp_kernel::banked_reset::{self, Ledger, Receipt};
use std::sync::{Mutex as StdMutex, PoisonError};
use tokio::io::AsyncWriteExt as _;

/// The sender the Host names for what it tells the sessions of `[agents] reset_tell`.
const SENDER: &str = "hotseat-reset";
/// What of the Host's environment the reset tool is given (the Python Host's set).
const KEEPS: &[&str] = &[
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
];

/// The tools whose reset runs now: one at a time each.
static RUNNING: StdMutex<Vec<String>> = StdMutex::new(Vec::new());

/// The owner's reset settings of a tool, as this Host's config has them: the command (empty
/// is off) and the cap per 24 hours (`None`: none; Codex's defaults to 1, 0 is off).
pub(crate) fn settings(tool: &str, file: &toml::Table) -> (Vec<String>, Option<u32>) {
    let agents = file.get("agents").and_then(toml::Value::as_table);
    let field = if tool == "codex" {
        "codex_reset_command"
    } else {
        "claude_reset_command"
    };
    let command: Vec<String> = agents
        .and_then(|agents| agents.get(field))
        .and_then(toml::Value::as_array)
        .map(|words| {
            words
                .iter()
                .filter_map(|word| word.as_str().map(str::to_string))
                .collect()
        })
        .filter(|words: &Vec<String>| {
            !words.is_empty() && words.len() <= 12 && words.iter().all(|w| !w.is_empty())
        })
        .unwrap_or_default();
    let cap = (tool == "codex").then(|| {
        agents
            .and_then(|agents| agents.get("codex_reset_max_per_day"))
            .and_then(toml::Value::as_integer)
            .map_or(1, |cap| u32::try_from(cap.max(0)).unwrap_or(0))
    });
    (command, cap)
}

/// The sessions told of a spent reset and of a reset that needs someone, beside the owner's
/// push (`[agents] reset_tell`, a list of session names; empty or absent: the push only).
pub(crate) fn told_sessions(file: &toml::Table) -> Vec<String> {
    file.get("agents")
        .and_then(toml::Value::as_table)
        .and_then(|agents| agents.get("reset_tell"))
        .and_then(toml::Value::as_array)
        .map(|names| {
            names
                .iter()
                .filter_map(toml::Value::as_str)
                .map(str::trim)
                .filter(|name| clarp_kernel::native_session_name(name))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

/// The owner's settings, from the folder `--config` named. A Host named none has no reset
/// command: one started by hand for a test never spends a banked reset of the owner's real
/// accounts (issue 780).
fn host_file() -> toml::Table {
    crate::config::host_config_file()
        .and_then(|path| std::fs::read_to_string(path).ok())
        .and_then(|text| text.parse::<toml::Table>().ok())
        .unwrap_or_default()
}

struct Claim(String);

impl Claim {
    fn take(tool: &str) -> Option<Self> {
        let mut running = RUNNING.lock().unwrap_or_else(PoisonError::into_inner);
        if running.iter().any(|t| t == tool) {
            return None;
        }
        running.push(tool.to_string());
        Some(Self(tool.to_string()))
    }
}

impl Drop for Claim {
    fn drop(&mut self) {
        RUNNING
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .retain(|t| *t != self.0);
    }
}

async fn ledger(app: &App, tool: &str) -> Option<Ledger> {
    let mut state = app.seats.state.lock().await;
    let kept = state.kept(app).await?;
    Some(kept.resets.get(tool).cloned().unwrap_or_default())
}

async fn keep(app: &App, tool: &str, ledger: Ledger) {
    let mut state = app.seats.state.lock().await;
    let Some(kept) = state.kept(app).await else {
        return;
    };
    kept.resets.insert(tool.to_string(), ledger);
    if let Ok(data) = serde_json::to_vec(kept) {
        app.fleet.keep_said(KIND, KEPT.into(), Some(data)).await;
    }
}

/// After a check of the switcher: whether to ask the tool's reset command, and if so, ask it
/// on a task of its own. `exhausted`: the switcher found no account with room. `body`: the
/// read it decided on. `models`: the models the agents need.
pub(super) async fn consider(
    app: &App,
    janitor: &str,
    tool: &str,
    exhausted: bool,
    body: &Value,
    accounts: Vec<String>,
    models: Vec<String>,
) {
    let file = host_file();
    let (command, cap) = settings(tool, &file);
    if command.is_empty() {
        return;
    }
    // The claim comes before the ledger is read: a run that ends in between would otherwise
    // leave this check deciding on a ledger from before its spend.
    let Some(claim) = Claim::take(tool) else {
        return;
    };
    let now = app.clock.now_ms();
    let Some(ledger) = ledger(app, tool).await else {
        return;
    };
    let reconcile_only = banked_reset::reconcile_due(&ledger, now);
    if !reconcile_only {
        if !exhausted || !banked_reset::due(tool, body, now) {
            return;
        }
        if banked_reset::refused(&ledger, cap, now).is_some() {
            return;
        }
    }
    let told = told_sessions(&file);
    let natural = banked_reset::natural_resets(tool, body, now).unwrap_or_default();
    let app = app.clone();
    let tool = tool.to_string();
    let janitor = janitor.to_string();
    tokio::spawn(async move {
        let _claim = claim;
        let models = if reconcile_only { Vec::new() } else { models };
        let receipt = run(&command, &models, &accounts, reconcile_only).await;
        let now = app.clock.now_ms();
        let mut ledger = ledger;
        let attention = ledger.after(&receipt, reconcile_only, now);
        keep(&app, &tool, ledger).await;
        if let Some(attention) = attention {
            let text = banked_reset::told_attention(&tool, attention);
            tell(&app, &janitor, &told, &tool, &text, now).await;
        }
        if !receipt.verified {
            if receipt.needs_reconcile {
                eprintln!(
                    "clarp-host: {tool} banked reset: {} (to be reconciled)",
                    receipt.status
                );
            }
            return;
        }
        let Some(account) = receipt.account.clone() else {
            return;
        };
        // The refilled account takes the turns, the way the switcher moves them.
        let _ = switching::perform(&app, &tool, &account, None).await;
        let text = banked_reset::told(&tool, &receipt, natural.get(&account).copied(), now);
        tell(&app, &janitor, &told, &tool, &text, now).await;
    });
}

/// Starts the reset command once, gives it the request and reads its receipt.
async fn run(
    command: &[String],
    models: &[String],
    saved: &[String],
    reconcile_only: bool,
) -> Receipt {
    let Some((program, args)) = command.split_first() else {
        return banked_reset::receipt(false, "", saved, reconcile_only);
    };
    let mut child = clarp_child::prepared(program, Some(KEEPS));
    child.args(args);
    {
        use std::os::unix::process::CommandExt as _;
        child.process_group(0);
    }
    let mut child = tokio::process::Command::from(child);
    child
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let Ok(mut process) = child.spawn() else {
        return banked_reset::receipt(false, "", saved, reconcile_only);
    };
    let mut request = json!({ "models": models });
    if reconcile_only {
        request["reconcile_only"] = json!(true);
    }
    if let Some(mut stdin) = process.stdin.take() {
        let _ = stdin.write_all(request.to_string().as_bytes()).await;
    }
    let pid = process.id();
    let finished = tokio::time::timeout(
        Duration::from_secs(banked_reset::TIMEOUT_SECONDS),
        process.wait_with_output(),
    )
    .await;
    match finished {
        Ok(Ok(output)) => banked_reset::receipt(
            output.status.success(),
            &String::from_utf8_lossy(&output.stdout),
            saved,
            reconcile_only,
        ),
        _ => {
            // Whatever it started goes with it.
            if let Some(pid) = pid.and_then(|pid| i32::try_from(pid).ok()) {
                let _ = nix::sys::signal::killpg(
                    nix::unistd::Pid::from_raw(pid),
                    nix::sys::signal::Signal::SIGKILL,
                );
            }
            banked_reset::receipt(false, "", saved, reconcile_only)
        }
    }
}

/// Tells the owner (a push from the Hotseat janitor's chat) and the sessions of
/// `[agents] reset_tell` that are on this Host.
async fn tell(app: &App, janitor: &str, told: &[String], tool: &str, text: &str, now: i64) {
    let id = format!("hotseat-reset-{tool}-{now}");
    let directory = app.fleet.directory();
    if let Some(member) = directory.janitor(janitor) {
        app.pusher.turn_done(crate::pusher::TurnDone {
            order: crate::pusher::Order::observe(app.clock.now_ms()),
            badge: false,
            unread: false,
            session: member.record.session.clone(),
            persona: member.record.persona.clone(),
            agent_id: member.record.agent_id.clone(),
            notification_id: id.clone(),
            preview: text.to_string(),
            needs_response: false,
            event: clarp_kernel::notify_events::Event::Account,
            ..crate::pusher::TurnDone::default()
        });
    }
    for session in told
        .iter()
        .filter(|session| directory.named(session).is_some())
    {
        let request = SendRequest {
            session: session.clone(),
            text: text.to_string(),
            client_msg_id: Some(format!("{id}-{session}")),
            force_session: Some(true),
            synthesize_audio: Some(false),
            hands_free: Some(false),
            origin: Some("agent".into()),
            sender: Some(json!(SENDER)),
            ..SendRequest::default()
        };
        let made = crate::coordination::Made {
            internal_sender: Some(SENDER),
            ..crate::coordination::Made::default()
        };
        let _ = crate::edge::send_from(app, request, &made).await;
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn the_settings_are_read_as_the_python_host_reads_them() {
        let file: toml::Table = r#"
[agents]
claude_reset_command = ["/bin/claude-auto-reset", "--broker", "laptopstudio"]
codex_reset_command = ["/bin/codex-auto-reset"]
"#
        .parse()
        .unwrap();
        assert_eq!(
            settings("claude", &file),
            (
                vec![
                    "/bin/claude-auto-reset".into(),
                    "--broker".into(),
                    "laptopstudio".into()
                ],
                None
            )
        );
        assert_eq!(
            settings("codex", &file),
            (vec!["/bin/codex-auto-reset".into()], Some(1))
        );
        let off: toml::Table = "[agents]\ncodex_reset_command = []\ncodex_reset_max_per_day = 0\n"
            .parse()
            .unwrap();
        assert_eq!(settings("codex", &off), (vec![], Some(0)));
        assert_eq!(settings("claude", &toml::Table::new()), (vec![], None));
    }

    #[test]
    fn who_is_told_is_a_setting() {
        let file: toml::Table = "[agents]\nreset_tell = [\"front-desk-1\", \" \", \"other\"]\n"
            .parse()
            .unwrap();
        assert_eq!(told_sessions(&file), ["front-desk-1", "other"]);
        let none: toml::Table = "[agents]\nreset_tell = []\n".parse().unwrap();
        assert!(told_sessions(&none).is_empty());
        assert!(told_sessions(&toml::Table::new()).is_empty());
    }

    #[test]
    fn a_check_while_a_run_of_the_tool_is_under_way_takes_no_claim() {
        // The claim is what consider() takes before it reads the ledger.
        let first = Claim::take("test-tool").unwrap();
        assert!(Claim::take("test-tool").is_none());
        let other = Claim::take("test-other").unwrap();
        drop(first);
        assert!(Claim::take("test-tool").is_some());
        drop(other);
    }

    #[tokio::test]
    async fn the_command_is_given_the_request_and_its_receipt_is_read() {
        let dir = std::env::temp_dir().join(format!("clarp-reset-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let script = dir.join("reset");
        let seen = dir.join("request.json");
        std::fs::write(
            &script,
            format!(
                "#!/bin/sh\ncat > '{}'\necho '{{\"status\":\"redeemed\",\"verified\":true,\"account\":\"b\",\"resets_left\":1}}'\n",
                seen.display()
            ),
        )
        .unwrap();
        let mut permissions = std::fs::metadata(&script).unwrap().permissions();
        std::os::unix::fs::PermissionsExt::set_mode(&mut permissions, 0o755);
        std::fs::set_permissions(&script, permissions).unwrap();
        let command = vec![script.display().to_string()];
        let got = run(&command, &["m".into()], &["a".into(), "b".into()], false).await;
        assert!(got.verified && got.account.as_deref() == Some("b"));
        let request: Value =
            serde_json::from_str(&std::fs::read_to_string(&seen).unwrap()).unwrap();
        assert_eq!(request, json!({"models": ["m"]}));
        let got = run(&command, &[], &["b".into()], true).await;
        let request: Value =
            serde_json::from_str(&std::fs::read_to_string(&seen).unwrap()).unwrap();
        assert_eq!(request, json!({"models": [], "reconcile_only": true}));
        // "redeemed" is no answer to a reconciliation.
        assert!(!got.verified && got.needs_reconcile);
        std::fs::remove_dir_all(dir).unwrap();
    }
}
