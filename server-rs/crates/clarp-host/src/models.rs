//! The models an app's picker offers: the tools this Host runs, asked what they have.
//!
//! `GET /agent-model-options` answers with a row for each tool: how it is shown, whether
//! it is on this machine and in which version, and the models it can run. The rows are
//! made by [`clarp_backend::catalogue`], by the Python Host's rules; here the tools are
//! asked. A tool's program is looked for where its turns would start it; it is asked for
//! its version (`--version`); Codex is asked for its models (`codex debug models`), and
//! of Claude Code the file is read in which it keeps what the account that is signed in
//! may run. Each has two and a half seconds, both tools are asked at once, and nothing of
//! it runs on a thread that serves. What was found is told again for five minutes without
//! asking, unless a login changed meanwhile: another account may run other models.
//!
//! The Python Host lists every tool it knows, also those that are not installed. This
//! Host lists the two it runs: an app offers an agent on each tool that is listed.
//!
//! What this Host cannot do with a tool yet, it does not say it can ([`NOT_YET`]): an app
//! reads a tool's row to know what to offer.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use axum::extract::State;
use axum::routing::get;
use axum::{Json, Router};
use clarp_backend::catalogue::{self, Found, Said};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::edge::App;

/// The tools this Host runs, in the order an app lists them.
const TOOLS: [&str; 3] = ["claude", "codex", "agy"];
/// How long a tool has to answer one question about itself, where nothing else is declared
/// of the question (Antigravity fetches its list of models, and is given longer).
const WITHIN: Duration = Duration::from_millis(2_500);
/// How long what was found is told again without asking.
const KEPT_FOR: Duration = Duration::from_secs(300);

/// What is declared of a tool and this Host cannot do with it yet. Claude Code: a turn is
/// not steered while it runs, a conversation is not made shorter from an app, a tool is
/// given no servers of its own, nobody signs in from an app, and what an account has used
/// is not read. Codex, beside those: a past session of it is not forked. Antigravity: a
/// conversation is not made shorter from an app.
const NOT_YET: [(&str, &[&str]); 3] = [
    (
        "claude",
        &[
            "supports_steer",
            "supports_compact",
            "supports_mcp",
            "supports_auth",
            "supports_usage",
        ],
    ),
    (
        "codex",
        &[
            "supports_fork",
            "supports_steer",
            "supports_compact",
            "supports_mcp",
            "supports_auth",
            "supports_usage",
        ],
    ),
    ("agy", &["supports_compact"]),
];

/// What was found, with when, and under which logins.
struct Kept {
    at: Instant,
    logins: [u8; 32],
    told: Value,
}

/// What was last found of the tools, kept for a while.
#[derive(Default)]
pub struct Shelf {
    kept: tokio::sync::Mutex<Option<Kept>>,
}

/// Where a tool's program is: a path that is a file, or the first of that name on the
/// path. `None`: it is not on this machine.
fn located(name: &str) -> Option<PathBuf> {
    if name.contains('/') {
        let path = PathBuf::from(name);
        return path.is_file().then_some(path);
    }
    let path = std::env::var_os("PATH").unwrap_or_default();
    std::env::split_paths(&path)
        .map(|dir| dir.join(name))
        .find(|candidate| candidate.is_file())
}

/// The file in which Claude Code keeps what its account may run.
fn claude_file(home: &Path) -> PathBuf {
    let named = std::env::var_os("CLAUDE_CONFIG_FILE").filter(|named| !named.is_empty());
    named.map_or_else(|| home.join(".claude.json"), PathBuf::from)
}

/// A file's text, whatever bytes it holds. `None`: there is none to read.
fn text_of(file: &Path) -> Option<String> {
    let bytes = std::fs::read(file).ok()?;
    Some(String::from_utf8_lossy(&bytes).into_owned())
}

/// What stands for the logins under which the tools were asked: what Claude Code's file
/// says of its account and of the models it may run (the file itself changes with every
/// start of the tool), and the file Codex keeps its login in. Only a digest, in memory.
fn logins(home: &Path) -> [u8; 32] {
    let mut all = Sha256::new();
    let kept = text_of(&claude_file(home)).unwrap_or_default();
    let kept: Value = serde_json::from_str(&kept).unwrap_or_default();
    let of_models = json!([
        kept["oauthAccount"]["accountUuid"],
        kept["oauthAccount"]["organizationUuid"],
        kept["modelAccessCache"],
        kept["additionalModelOptionsCache"],
    ]);
    all.update(of_models.to_string().as_bytes());
    let codex = crate::logins::files(home).remove("codex");
    let codex = codex.and_then(|file| std::fs::read(file).ok());
    all.update(Sha256::digest(codex.unwrap_or_default()));
    all.finalize().into()
}

/// Starts a tool for one answer about itself: how it ended and what it printed. What it
/// writes aside is not read. `None`: it could not be started, or did not answer in time.
async fn asked(program: &Path, args: &[String], within: Duration) -> Option<(i32, String)> {
    let mut command = tokio::process::Command::new(program);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    // The Host's own secrets stay with the Host, as for a turn.
    for name in crate::OWN_SECRETS {
        command.env_remove(name);
    }
    let child = command.spawn().ok()?;
    let done = tokio::time::timeout(within, child.wait_with_output()).await;
    let done = done.ok()?.ok()?;
    let printed = String::from_utf8_lossy(&done.stdout).into_owned();
    Some((done.status.code().unwrap_or(-1), printed))
}

/// A tool's row, from what the tool says of itself now.
async fn row_of(tool: &str, home: &Path, observed_at: &str) -> Value {
    let program = clarp_backend::program(tool).and_then(|name| located(&name));
    let how = catalogue::declared(tool)["asked"].clone();
    let for_its_version = ["--version".to_string()];
    let (version, models) = match &program {
        None => (None, None),
        Some(program) => {
            let version = asked(program, &for_its_version, WITHIN);
            // How long the tool is given for its models, where that is declared of it.
            let declared = how["within_s"].as_f64().filter(|seconds| *seconds > 0.0);
            let within = declared.map_or(WITHIN, Duration::from_secs_f64);
            let models = async {
                match how["how"].as_str() {
                    // Asked by a command: its answer counts where it ended well.
                    Some("command") => {
                        let with: Vec<String> = how["with"]
                            .as_array()
                            .into_iter()
                            .flatten()
                            .filter_map(|word| word.as_str().map(str::to_string))
                            .collect();
                        let said = asked(program, &with, within).await;
                        said.filter(|(exit, _)| *exit == 0).map(|(_, out)| out)
                    }
                    // Kept in a file of the tool's own.
                    Some("file") => {
                        let file = claude_file(home);
                        let read = tokio::task::spawn_blocking(move || text_of(&file));
                        read.await.ok().flatten()
                    }
                    _ => None,
                }
            };
            tokio::join!(version, models)
        }
    };
    let program = program.map(|path| path.display().to_string());
    let found = Found {
        program: program.as_deref(),
        version: version.as_ref().map(|(exit, out)| Said {
            exit: *exit,
            out,
            err: "",
        }),
        models: models.as_deref(),
    };
    let mut row = catalogue::row(tool, &found, observed_at);
    let not_yet = NOT_YET.iter().find(|(name, _)| *name == tool);
    for flag in not_yet.map_or(&[][..], |(_, flags)| *flags) {
        row[*flag] = json!(false);
    }
    row
}

impl Shelf {
    /// The catalogue: what was found within the last five minutes under the logins that
    /// are there now, or what the tools say when they are asked now. Who asks while the
    /// tools are being asked waits for that answer and is given it.
    pub async fn told(&self, now_ms: i64) -> Value {
        let home = crate::config::home();
        let of = home.clone();
        let logins = tokio::task::spawn_blocking(move || logins(&of)).await;
        let logins = logins.unwrap_or_default();
        let mut kept = self.kept.lock().await;
        if let Some(kept) = kept.as_ref()
            && kept.at.elapsed() <= KEPT_FOR
            && kept.logins == logins
        {
            return kept.told.clone();
        }
        let observed_at = crate::wire::to_the_second(now_ms);
        let asked = TOOLS.map(|tool| row_of(tool, &home, &observed_at));
        let found = futures_util::future::join_all(asked).await;
        let named = TOOLS.iter().map(|tool| (*tool).to_string());
        let rows: Vec<(String, Value)> = named.zip(found).collect();
        let told = catalogue::whole(rows, &observed_at);
        *kept = Some(Kept {
            at: Instant::now(),
            logins,
            told: told.clone(),
        });
        told
    }
}

/// `GET /agent-model-options`: the tools this Host runs and the models each can run.
async fn told(State(app): State<App>) -> Json<Value> {
    Json(app.models.told(app.clock.now_ms()).await)
}

pub fn routes() -> Router<App> {
    Router::new().route("/agent-model-options", get(told))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_moment_is_written_to_the_second() {
        let to_the_second = crate::wire::to_the_second;
        assert_eq!(to_the_second(1_800_000_000_999), "2027-01-15T08:00:00Z");
        assert_eq!(to_the_second(0), "1970-01-01T00:00:00Z");
    }

    #[test]
    fn what_is_not_built_is_named_by_flags_the_catalogue_has() {
        for (tool, flags) in NOT_YET {
            let row = catalogue::row(tool, &Found::default(), "2027-01-15T08:00:00Z");
            for flag in flags {
                assert!(row[*flag].is_boolean(), "{tool}: {flag}");
            }
        }
    }
}
