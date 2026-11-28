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
const TOOLS: [&str; 8] = [
    "claude", "codex", "agy", "agy-acp", "grok", "opencode", "deepseek", "muse",
];
/// How long a tool has to answer one question about itself, where nothing else is declared
/// of the question (Antigravity fetches its list of models, and is given longer).
const WITHIN: Duration = Duration::from_millis(2_500);
/// How long what was found is told again without asking.
const KEPT_FOR: Duration = Duration::from_secs(300);
/// The efforts Antigravity ends a model's name in, which it offers over ACP as a thinking
/// level apart from the model.
const ACP_EFFORTS: [&str; 5] = ["minimal", "low", "medium", "high", "xhigh"];

/// What is declared of a tool and this Host does not offer from an app, or cannot do with it
/// yet. Claude Code: steering is not advertised, as the Python Host does not advertise it
/// either; a message sent to a running turn is still read into it (`clarp-kernel`
/// `steering.rs`). Nobody signs in from an app (but on the Host that keeps the machine's
/// tools: `told`), and what an account has used is not read. Codex, beside those: a past session of
/// it is not forked, and its conversation is not made shorter from an app. Antigravity: a
/// conversation is not made shorter from an app; over ACP, steering is not advertised, as for
/// Claude Code, and a message sent to a running turn is read into it all the same.
const NOT_YET: [(&str, &[&str]); 5] = [
    (
        "claude",
        &["supports_steer", "supports_auth", "supports_usage"],
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
    // Antigravity over ACP does not advertise steering, whatever the `agy` row it is made
    // from says: as for Claude Code, a message sent while its turn runs is offered to that turn
    // by the Host (`Backend::steers`), and no app asks for it.
    ("agy-acp", &["supports_compact", "supports_steer"]),
    ("grok", &["supports_compact", "supports_auth"]),
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
    kept: std::sync::Mutex<Option<Kept>>,
    reading: tokio::sync::Mutex<()>,
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
pub(crate) async fn asked(
    program: &Path,
    args: &[String],
    within: Duration,
) -> Option<(i32, String)> {
    let mut command = crate::child::asynchronous(program);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let child = command.spawn().ok()?;
    let done = tokio::time::timeout(within, child.wait_with_output()).await;
    let done = done.ok()?.ok()?;
    let printed = String::from_utf8_lossy(&done.stdout).into_owned();
    Some((done.status.code().unwrap_or(-1), printed))
}

/// A tool's row, from what the tool says of itself now.
async fn row_of(
    tool: &str,
    home: &Path,
    observed_at: &str,
    listing: &tokio::sync::OnceCell<Option<String>>,
) -> Value {
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
                    Some("listing") => listing
                        .get_or_init(|| async {
                            let args = ["models".to_string(), "--verbose".to_string()];
                            asked(program, &args, WITHIN)
                                .await
                                .filter(|(code, _)| *code == 0)
                                .map(|(_, text)| text)
                        })
                        .await
                        .clone(),
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
                    Some("directory") if tool == "muse" => {
                        let root = clarp_backend::past::native_home(
                            clarp_kernel::grok_muse::Tool::Muse,
                            home,
                        );
                        tokio::task::spawn_blocking(move || {
                            let mut paths: Vec<_> = std::fs::read_dir(root.join("model-catalog"))
                                .ok()?
                                .flatten()
                                .map(|e| e.path())
                                .filter(|p| p.extension().is_some_and(|e| e == "json"))
                                .collect();
                            paths.sort();
                            let mut rows = Vec::new();
                            for path in paths {
                                let Some(raw) = text_of(&path) else { break };
                                let data: Value = serde_json::from_str(&raw).unwrap_or_default();
                                rows.extend(data["rows"].as_array().into_iter().flatten().cloned());
                            }
                            Some(json!({"rows":rows}).to_string())
                        })
                        .await
                        .ok()
                        .flatten()
                    }
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
    if matches!(tool, "opencode" | "deepseek") {
        let home = home.to_owned();
        let deepseek = tool == "deepseek";
        let joined = tokio::task::spawn_blocking(move || {
            crate::logins::opencode::group_models(&home, row, deepseek)
        });
        return joined.await.unwrap_or_default();
    }
    row
}

impl Shelf {
    /// Forgets what was found, so that the next who asks has the tools asked again: a
    /// provider was signed in or out, and OpenCode may run other models
    /// (`harness_providers`).
    pub fn forget(&self) {
        *self
            .kept
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = None;
    }

    /// Why a tool has no model of this name, checked against what the tool listed when it
    /// was last asked, or for Antigravity, where it has not been, against the models it is
    /// known to have. Codex is not held to a list it never gave. Antigravity over ACP is held
    /// to what `agy models` listed, the models its ACP agent offers, and takes a model of it
    /// also named without its effort (`gemini-4-neon`), as ACP offers the model and its
    /// thinking level apart; where nothing was listed it is not held to the known models,
    /// and its turn checks the name against what Antigravity offers over ACP. Only what was
    /// found already is read: [`Shelf::model_error`] asks the tools first.
    pub fn policy_model_error(&self, tool: &str, model: &str) -> Option<String> {
        if !matches!(tool, "codex" | "agy" | "agy-acp") {
            return None;
        }
        let kept = self
            .kept
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let observed = kept
            .as_ref()
            .map(|k| &k.told["providers"][tool])
            .filter(|r| r["source"]["kind"] == "cli_probe");
        let bundled = catalogue::declared(tool);
        let row = observed.or_else(|| (tool == "agy").then_some(&bundled))?;
        let mut ids: Vec<_> = row["models"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|item| item["id"].as_str())
            .collect();
        let model = model.trim();
        // A model of the list without the effort its name ends in.
        let without_effort = |id: &&str| {
            id.strip_prefix(model)
                .and_then(|rest| rest.strip_prefix('-'))
                .is_some_and(|effort| ACP_EFFORTS.contains(&effort))
        };
        if ids.contains(&model)
            || (ids.is_empty() && tool == "codex")
            || (tool == "agy-acp" && ids.iter().any(without_effort))
        {
            return None;
        }
        let reason = "Invalid provider model identifier";
        ids.sort_unstable();
        Some(format!(
            "{reason}: {tool} has no model '{model}'; it offers {}",
            ids.join(", ")
        ))
    }

    /// The same, after asking the tools where what was found does not have the model: since
    /// this Host started nobody may have asked them (an app asks when it opens its picker),
    /// or a tool may list a model it did not have when it was asked more than five minutes
    /// ago. A model that is there is let through without asking anything.
    pub async fn model_error(&self, tool: &str, model: &str, now_ms: i64) -> Option<String> {
        self.policy_model_error(tool, model)?;
        self.told(now_ms).await;
        self.policy_model_error(tool, model)
    }

    /// Has the tools asked, where nothing has been found of them since this Host started, so
    /// that a check that cannot wait for them ([`Shelf::policy_model_error`]) reads what they
    /// list and not only what is known of them.
    pub async fn found_once(&self, now_ms: i64) {
        let found = self
            .kept
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .is_some();
        if !found {
            self.told(now_ms).await;
        }
    }

    /// The catalogue: what was found within the last five minutes under the logins that
    /// are there now, or what the tools say when they are asked now. Who asks while the
    /// tools are being asked waits for that answer and is given it.
    pub async fn told(&self, now_ms: i64) -> Value {
        let home = crate::config::home();
        let of = home.clone();
        let logins = tokio::task::spawn_blocking(move || logins(&of)).await;
        let logins = logins.unwrap_or_default();
        let _reader = self.reading.lock().await;
        if let Some(kept) = self
            .kept
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .as_ref()
            && kept.at.elapsed() <= KEPT_FOR
            && kept.logins == logins
        {
            return kept.told.clone();
        }
        let observed_at = crate::wire::to_the_second(now_ms);
        let listing = tokio::sync::OnceCell::new();
        let asked = TOOLS.map(|tool| row_of(tool, &home, &observed_at, &listing));
        let found = futures_util::future::join_all(asked).await;
        let named = TOOLS.iter().map(|tool| (*tool).to_string());
        let rows: Vec<(String, Value)> = named.zip(found).collect();
        let told = catalogue::whole(rows, &observed_at);
        *self
            .kept
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(Kept {
            at: Instant::now(),
            logins,
            told: told.clone(),
        });
        told
    }
}

/// The configured model for a backend when the agent has selected none.
pub(crate) async fn default_for(app: &App, backend: &str) -> Option<String> {
    let key = format!("{backend}_model");
    if let Some(model) = crate::owner::text_at(&app.mouth.owners(), &["agents", &key]) {
        return Some(model);
    }
    let backend = backend.to_owned();
    tokio::task::spawn_blocking(move || {
        let home = crate::config::home();
        match backend.as_str() {
            "claude" => {
                let config = std::env::var_os("CLAUDE_CONFIG_DIR")
                    .filter(|directory| !directory.is_empty())
                    .map_or_else(|| home.join(".claude"), PathBuf::from);
                std::env::var("ANTHROPIC_MODEL")
                    .ok()
                    .filter(|model| !model.is_empty())
                    .or_else(|| {
                        let settings =
                            std::fs::read_to_string(config.join("settings.json")).ok()?;
                        let settings: Value = serde_json::from_str(&settings).ok()?;
                        settings["model"].as_str().map(str::to_owned)
                    })
            }
            "codex" => {
                let config = crate::past::codex_home(&home).join("config.toml");
                let config = std::fs::read_to_string(config).ok()?;
                let config: toml::Table = config.parse().ok()?;
                let profile = crate::owner::text_at(&config, &["profile"]);
                profile
                    .as_deref()
                    .and_then(|profile| {
                        crate::owner::text_at(&config, &["profiles", profile, "model"])
                    })
                    .or_else(|| crate::owner::text_at(&config, &["model"]))
            }
            _ => None,
        }
    })
    .await
    .ok()
    .flatten()
}

/// `GET /agent-model-options`: the tools this Host runs and the models each can run. The Host
/// that keeps the machine's tools signs them in from an app, and says so of each it can.
async fn told(State(app): State<App>) -> Json<Value> {
    let (mut told, auth) = tokio::join!(
        app.models.told(app.clock.now_ms()),
        crate::logins::sign_in::rows(&app, true, true),
    );
    if let Ok(auth) = auth {
        for status in auth["backends"].as_array().into_iter().flatten() {
            let Some(row) = status["id"]
                .as_str()
                .and_then(|id| told["providers"].get_mut(id))
            else {
                continue;
            };
            let authenticated = if status["logged_in"] == true {
                Some(true)
            } else if status["installed"] == false
                || matches!(status["state"].as_str(), Some("signed_out" | "expired"))
            {
                Some(false)
            } else {
                None
            };
            row["authenticated"] = json!(authenticated);
            row["authentication_source"]["detail"] = json!("backend-auth");
            if authenticated.is_some() {
                row["authentication_source"]["kind"] = json!("backend_auth");
            }
        }
    }
    // Which tool an agent can be moved to, and the model each runs when none is named.
    crate::agent_transit::annotate(&app, &mut told).await;
    if let Some(providers) = told["providers"].as_object_mut() {
        for (tool, row) in providers {
            let harness = clarp_backend::by_id(tool).and_then(|backend| backend.runs_through());
            row["runs_through"] = json!(harness);
        }
    }
    if crate::logins::sign_in::keeps(&app) {
        for tool in clarp_kernel::sign_in::TOOLS {
            if let Some(row) = told["providers"].get_mut(tool) {
                row["supports_auth"] = json!(true);
            }
        }
    }
    Json(told)
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

    /// A shelf that holds what the tools were found to have, as `told` keeps it.
    fn shelf_with(rows: Vec<(String, Value)>) -> Shelf {
        let shelf = Shelf::default();
        *shelf.kept.lock().unwrap() = Some(Kept {
            at: Instant::now(),
            logins: [0; 32],
            told: catalogue::whole(rows, "2027-01-15T08:00:00Z"),
        });
        shelf
    }

    #[test]
    fn antigravity_is_held_to_what_it_lists_and_its_refusal_names_them() {
        let said = "gemini-5-x-low\tX (Low)\ngemini-5-x-high\tX\n";
        let found = Found {
            program: Some("/synthetic/bin/agy"),
            version: None,
            models: Some(said),
        };
        let at = "2027-01-15T08:00:00Z";
        let shelf = shelf_with(vec![
            ("agy".to_string(), catalogue::row("agy", &found, at)),
            ("agy-acp".to_string(), catalogue::row("agy-acp", &found, at)),
        ]);
        for tool in ["agy", "agy-acp"] {
            assert_eq!(shelf.policy_model_error(tool, "gemini-5-x-low"), None);
            let refused = shelf
                .policy_model_error(tool, "gemini-4-neon-medium")
                .unwrap();
            assert!(
                refused.ends_with("it offers gemini-5-x-high, gemini-5-x-low"),
                "{refused}"
            );
        }
        // Over ACP a model of the list is also named without its effort, and nothing else.
        assert_eq!(shelf.policy_model_error("agy-acp", "gemini-5-x"), None);
        assert!(shelf.policy_model_error("agy", "gemini-5-x").is_some());
        assert!(shelf.policy_model_error("agy-acp", "gemini-5").is_some());
    }

    #[test]
    fn antigravity_not_yet_asked_is_held_to_the_models_it_is_known_to_have() {
        let shelf = Shelf::default();
        assert_eq!(
            shelf.policy_model_error("agy", "gemini-4-neon-medium"),
            None
        );
        let refused = shelf.policy_model_error("agy", "gemini-nothing").unwrap();
        assert!(refused.contains("gemini-4-neon-medium"), "{refused}");
        // Over ACP, with nothing listed, the turn checks the model against what is offered.
        assert_eq!(shelf.policy_model_error("agy-acp", "gemini-nothing"), None);
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
