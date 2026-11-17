//! The use of tools explained in plain words: the desk at the Host's edge.
//!
//! The rules are the kernel's ([`clarp_kernel::explain`]): how a use is read, what is said
//! of it without a model, what was learned, and the desk where an app asks and is
//! answered. Here is what the desk needs of a Host: its book is read where it is kept the
//! first time it is asked for (what was learned, row by row, and the hours of its ledger,
//! with the fleet; the key shapes are signed with, drawn once, among the settings), the
//! janitor that explains is looked up in the janitors' book for the agent a request is
//! about, the scripts a call names are read in that agent's directory, and whatever the
//! desk says has to be kept, is kept.
//!
//! **Who asks the model.** A task looks at the desk five times a second while anything
//! waits there. What is due it hands to the tool the janitor is set to, in a run of that
//! janitor, as the Python Host does (`ToolExplanations._drain`): the janitors' book shows
//! the run, a janitor that is at another request is asked a moment later, and one that
//! was paused or set otherwise meanwhile is not asked at all. The tool is started to read
//! and nothing else ([`crate::once::read`]): what agents did is whatever stood in their
//! work, and is shown to a model that has nothing to act with.

use std::collections::{BTreeMap, HashSet};
use std::io::Read as _;
use std::os::unix::fs::OpenOptionsExt as _;
use std::path::{Path, PathBuf};
use std::time::Duration;

use axum::http::StatusCode;
use clarp_backend::Reading;
use clarp_kernel::explain::desk::{self as rules, Asked, Batch, Change, Janitor, Pace, Said};
use clarp_kernel::explain::learned::{self, Row};
use clarp_kernel::explain::read;
use clarp_kernel::janitors::book::{Book, World};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::agent::Unavailable;
use crate::edge::{ApiError, App};
use crate::once::{self, Unanswered};

/// The job of the janitor that explains.
pub(crate) const JOB: &str = "tool-explainer";
/// The kinds of thing the desk leaves with the fleet: a row of what was learned, and an
/// hour of the ledger.
const LEARNED: &str = "explained";
const COUNTED: &str = "explain-counted";
/// The setting the key is kept under that shapes are signed with.
const KEY: &str = "tool_explanations.identity_key";
/// How long a model is given for one batch.
const WITHIN: Duration = Duration::from_secs(45);
/// How often the desk is looked at while something waits, and how long a batch waits
/// whose janitor is at another request.
const LOOKS_EVERY: Duration = Duration::from_millis(200);
const BUSY_MS: i64 = 250;
/// How large a script may be to be read at all, and how much of it is read.
const SCRIPT_MOST: u64 = 65_536;
const SCRIPT_READ: u64 = 8_192;
const SCRIPTS_MOST: usize = 2;

/// The explainer of this Host: its book. `None` until it was read where it is kept.
#[derive(Default)]
pub struct Desk {
    held: tokio::sync::Mutex<Option<rules::Desk>>,
    wake: tokio::sync::Notify,
}

fn unread() -> Unavailable {
    Unavailable("the explainer's book could not be read".to_string())
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// The name a row is kept under: of its signature and its reader, and nothing that a
/// store could not take for a name.
fn place(signature: &str, level: u8) -> String {
    let digest = Sha256::digest(format!("{signature}\0{level}").as_bytes());
    hex(&digest[..20])
}

/// Keeps what a call to the desk changed: each row and each hour as it is now, once. What
/// cannot be kept is learned or counted again; an explanation is not worth failing for.
async fn keep(app: &App, changes: Vec<Change>) {
    let mut last: BTreeMap<(&'static str, String), Option<Vec<u8>>> = BTreeMap::new();
    for change in changes {
        match change {
            Change::Learned(row) => {
                if let Ok(written) = serde_json::to_vec(&row) {
                    last.insert((LEARNED, place(&row.signature, row.level)), Some(written));
                }
            }
            Change::Forgotten { signature, level } => {
                last.insert((LEARNED, place(&signature, level)), None);
            }
            Change::Counted {
                hour,
                counts,
                latency_ms,
            } => {
                if let Ok(written) = serde_json::to_vec(&(counts, latency_ms)) {
                    last.insert((COUNTED, format!("{hour:013}")), Some(written));
                }
            }
        }
    }
    for ((kind, key), value) in last {
        let _ = app.fleet.keep(kind, key, value).await;
    }
}

/// The janitor that explains for an agent, as the desk is told of it: the one that
/// answers requests for that agent now, else (paused, or none in whose scope the agent
/// is) the Host's own, which then explains nothing but still says which model it would
/// ask. What it is, is written into its identity, so that whoever asks a model later
/// knows whom for and with what.
fn janitor_of(book: &Book, world: &dyn World, target: &str, now_ms: i64) -> Option<Janitor> {
    let running = book.resolve(JOB, Some(target), world).ok().flatten();
    let own = || {
        let listed = book.listed(world).into_iter();
        let mut own = listed.filter(|key| {
            let shown = book.shown(key, world, now_ms);
            shown.is_ok_and(|shown| shown["builtin_role"] == JOB)
        });
        own.next()
    };
    let configured = running.clone().or_else(own)?;
    let shown = book.shown(&configured, world, now_ms).ok()?;
    let options = &shown["options"];
    let chosen = shown["configured_option_keys"].as_array();
    let chosen = chosen.is_some_and(|keys| keys.iter().any(|key| key == "detail_level"));
    // The Host's own janitor explains for the reader each device asks for, until its owner
    // chooses one for all of them.
    let each_its_own = shown["builtin_role"] == JOB && !chosen;
    let small = |value: &Value| value.as_u64().and_then(|value| u8::try_from(value).ok());
    let identity = running.map(|janitor| {
        let what = json!({
            "janitor": janitor, "target": target, "generation": shown["generation"],
            "backend": shown["backend"], "model": shown["model"], "effort": shown["effort"],
            "scope": shown["scope"], "options": options, "execution": shown["execution"],
        });
        what.to_string()
    });
    Some(Janitor {
        model: shown["model"].as_str().unwrap_or_default().to_string(),
        identity,
        level: (!each_its_own).then(|| small(&options["detail_level"]).unwrap_or(0)),
        sources: small(&options["explanation_sources"]).unwrap_or(0),
    })
}

/// What is read of the scripts a call names, in its agent's directory: only what lies
/// there plainly (no link on the way, nothing hidden, nothing of the system's), is a file
/// of no great size and holds text; its beginning; each script once, two at most. As the
/// Python Host reads them (`tool_explanations.script_evidence`).
fn scripts_read(cwd: &Path, activity: &Map<String, Value>) -> Vec<Value> {
    let Ok(root) = cwd.canonicalize() else {
        return Vec::new();
    };
    let mut read_of = Vec::new();
    let mut seen: Vec<PathBuf> = Vec::new();
    for name in read::script_names(activity) {
        let named = Path::new(&name);
        let path = if named.is_absolute() {
            named.to_path_buf()
        } else {
            root.join(named)
        };
        let Ok(real) = path.canonicalize() else {
            continue;
        };
        let part = |part: std::path::Component<'_>| part.as_os_str().to_string_lossy().into_owned();
        let parts: Vec<String> = real.components().map(part).collect();
        let of_the_system = ["proc", "sys", "dev", "run"];
        let apart = parts
            .get(1)
            .is_some_and(|top| of_the_system.contains(&top.as_str()));
        let hidden = parts.iter().any(|part| part.starts_with('.'));
        if real != path || apart || hidden || seen.contains(&real) {
            continue;
        }
        let Some(beginning) = beginning_of(&real) else {
            continue;
        };
        let file = parts.last().cloned().unwrap_or_default();
        read_of.push(read::script_excerpt(&file, &beginning));
        seen.push(real);
        if read_of.len() == SCRIPTS_MOST {
            break;
        }
    }
    read_of
}

/// The beginning of a file that is a file, of no great size, and text.
fn beginning_of(path: &Path) -> Option<String> {
    let flags = nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK;
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(flags)
        .open(path)
        .ok()?;
    let about = file.metadata().ok()?;
    if !about.is_file() || about.len() > SCRIPT_MOST {
        return None;
    }
    let mut holds = Vec::new();
    file.take(SCRIPT_READ).read_to_end(&mut holds).ok()?;
    if holds.contains(&0) {
        return None;
    }
    Some(String::from_utf8_lossy(&holds).into_owned())
}

impl Desk {
    /// The book, read where it is kept the first time it is asked for.
    async fn held(
        &self,
        app: &App,
    ) -> Result<tokio::sync::MutexGuard<'_, Option<rules::Desk>>, Unavailable> {
        let mut held = self.held.lock().await;
        if held.is_none() {
            let kept = app.fleet.directory().setting(KEY).map(str::to_string);
            let key = match kept.filter(|key| !key.is_empty()) {
                Some(key) => key,
                None => {
                    // Drawn once: what is learned under it is found under no other.
                    let key = crate::ids::hex(32);
                    app.fleet.set(vec![(KEY.to_string(), key.clone())]).await?;
                    key
                }
            };
            let rows = app.fleet.kept_all(LEARNED).await?;
            let rows = rows
                .iter()
                .filter_map(|(_, kept)| serde_json::from_slice::<Row>(kept).ok());
            let hours = app.fleet.kept_all(COUNTED).await?;
            let hours = hours.iter().filter_map(|(hour, kept)| {
                let (counts, latency_ms) = serde_json::from_slice::<([u64; 8], u64)>(kept).ok()?;
                Some((hour.parse::<i64>().ok()?, counts, latency_ms))
            });
            let (rows, hours): (Vec<Row>, Vec<_>) = (rows.collect(), hours.collect());
            *held = Some(rules::Desk::new(&key, Pace::default(), rows, hours));
        }
        Ok(held)
    }

    /// An app asks for the use of tools to be explained (`POST /tool-explanations`, what it
    /// sent): answered as the desk answers, for the agent the request names.
    pub(crate) async fn asked(&self, app: &App, asked: &Value) -> Result<Value, ApiError> {
        let directory = app.fleet.directory();
        let member = asked["session"]
            .as_str()
            .and_then(|session| directory.named(session));
        let Some(member) = member else {
            return Err(ApiError::new(StatusCode::NOT_FOUND, "agent not found"));
        };
        let target = member.record.agent_id.clone();
        let cwd = PathBuf::from(&member.record.cwd);
        let looked = app.janitors.with(app, |book, world, now_ms| {
            janitor_of(book, world, &target, now_ms)
        });
        let janitor = looked.await?;
        let request = Asked {
            level: &asked["detail_level"],
            items: &asked["items"],
            release: asked.get("release"),
            target: None,
            provenance: asked["include_provenance"] == true,
        };
        let mut held = self.held(app).await?;
        let desk = held.as_mut().ok_or_else(unread)?;
        let scripts = |activity: &Map<String, Value>| scripts_read(&cwd, activity);
        let answered = desk.request(&request, janitor.as_ref(), &scripts, app.clock.now_ms());
        let changes = desk.changes();
        drop(held);
        keep(app, changes).await;
        self.wake.notify_one();
        answered.map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))
    }

    /// How the explaining went (`GET /tool-explanations/stats`).
    pub(crate) async fn stats(
        &self,
        app: &App,
        window: &str,
        bucket: &str,
    ) -> Result<Value, ApiError> {
        let held = self.held(app).await?;
        let desk = held.as_ref().ok_or_else(unread)?;
        let said = desk.stats(window, bucket, app.clock.now_ms());
        said.map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))
    }

    /// One look at the desk: what is due is asked of the model, and what it says is told
    /// to the desk. Answers whether there may be more to do right away.
    async fn look(&self, app: &App) -> bool {
        let waiting = {
            let held = self.held.lock().await;
            match held.as_ref() {
                Some(desk) if desk.busy() => desk.asked_of(),
                _ => return false,
            }
        };
        // Which janitors are still what they were when something was asked of them.
        let mut still: HashSet<String> = HashSet::new();
        for identity in waiting {
            let said: Value = serde_json::from_str(&identity).unwrap_or_default();
            let target = said["target"].as_str().unwrap_or_default().to_string();
            let looked = app.janitors.with(app, |book, world, now_ms| {
                janitor_of(book, world, &target, now_ms)
            });
            let now = looked
                .await
                .ok()
                .flatten()
                .and_then(|janitor| janitor.identity);
            if now.as_deref() == Some(identity.as_str()) {
                still.insert(identity);
            }
        }
        let batch = {
            let mut held = self.held.lock().await;
            let Some(desk) = held.as_mut() else {
                return false;
            };
            let batch = desk.claim(&|identity| still.contains(identity), app.clock.now_ms());
            let changes = desk.changes();
            drop(held);
            keep(app, changes).await;
            batch
        };
        let Some(batch) = batch else {
            return false;
        };
        let said = ask(app, &batch).await;
        let mut held = self.held.lock().await;
        let Some(desk) = held.as_mut() else {
            return false;
        };
        let now = app.clock.now_ms();
        match &said {
            Some(said) => desk.complete(said, now),
            None => desk.abandon(now, BUSY_MS),
        }
        let changes = desk.changes();
        drop(held);
        keep(app, changes).await;
        said.is_some()
    }
}

/// Asks the model about a batch, in a run of the janitor it was asked of. `None`: the
/// janitor is at another request, and nothing was asked.
async fn ask(app: &App, batch: &Batch) -> Option<Said> {
    let of: Value = serde_json::from_str(&batch.identity).unwrap_or_default();
    let target = of["target"].as_str().unwrap_or_default().to_string();
    let request = json!(crate::ids::uuid());
    let asked_about = Value::Array(batch.requests.clone()).to_string();
    let context = json!({
        "request_hash": hex(&Sha256::digest(asked_about.as_bytes())),
        "item_count": batch.requests.len(), "detail_level": batch.level,
    });
    let begun = app.janitors.with(app, |book, world, now_ms| {
        let run = book.begin_run(JOB, &request, Some(&context), Some(&target), world, now_ms);
        let run_id = run.ok().flatten()?["run_id"].as_str()?.to_string();
        book.claim_run(&run_id, world, now_ms).then_some(run_id)
    });
    let run_id = begun.await.ok().flatten()?;
    let word = |key: &str| {
        of[key]
            .as_str()
            .filter(|word| !word.is_empty())
            .map(str::to_string)
    };
    let reading = Reading {
        instructions: rules::instructions(batch.level),
        asked: rules::asked(&batch.requests),
        schema: rules::schema(&batch.requests, false).to_string(),
        model: word("model"),
        effort: word("effort"),
    };
    let tool = word("backend").unwrap_or_else(|| "codex".to_string());
    let said = match once::read(&tool, &reading, WITHIN).await {
        Ok(answer) => {
            let read = clarp_kernel::router::extract_json(&answer).ok();
            let table = read.and_then(|read| rules::tabled(&Value::Object(read)));
            match table {
                None => Said::Failed("invalid_response"),
                Some(table) if learned::answers(&table, batch.requests.len()).is_none() => {
                    Said::Failed("translator_failed")
                }
                Some(table) => Said::Answered(table),
            }
        }
        Err(Unanswered::TimedOut) => Said::Failed("timeout"),
        Err(Unanswered::NotStarted(_)) => Said::Failed("codex_unavailable"),
        Err(_) => Said::Failed("translator_failed"),
    };
    let count = batch.requests.len();
    let (outcome, result, error) = match &said {
        Said::Answered(_) => (
            "completed",
            json!({ "summary": "Generated tool explanations", "status": "ready", "item_count": count }),
            "",
        ),
        Said::Failed(reason) => (
            "failed",
            json!({
                "summary": "Tool explanation generation failed", "status": "failed",
                "item_count": count, "reason": reason,
            }),
            *reason,
        ),
    };
    let told = app.janitors.with(app, |book, world, now_ms| {
        book.complete_run(&run_id, outcome, Some(&result), error, None, world, now_ms)
    });
    let _ = told.await;
    Some(said)
}

/// Starts the task that asks the model for what waits at the desk.
pub fn run(app: &App) {
    let app = app.clone();
    tokio::spawn(async move {
        loop {
            tokio::select! {
                () = tokio::time::sleep(LOOKS_EVERY) => {}
                () = app.explain.wake.notified() => {}
            }
            while app.explain.look(&app).await {}
        }
    });
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_script_is_read_where_it_lies_plainly_and_nowhere_else() {
        let root = std::env::temp_dir().join(format!("clarp-explain-{}", crate::ids::hex(6)));
        std::fs::create_dir_all(root.join("tools")).unwrap();
        std::fs::write(root.join("run.py"), "print('rows')\n").unwrap();
        std::fs::write(root.join("tools/sync.sh"), "#!/bin/sh\n").unwrap();
        std::fs::write(root.join(".hidden.py"), "print(1)\n").unwrap();
        std::fs::write(root.join("big.py"), vec![b'x'; 70_000]).unwrap();
        std::fs::write(root.join("binary.py"), [1u8, 0, 2]).unwrap();
        std::os::unix::fs::symlink(root.join("run.py"), root.join("link.py")).unwrap();
        let names = |command: &str| {
            let activity = json!({ "name": "Bash", "command": command });
            let read_of = scripts_read(&root, activity.as_object().unwrap());
            let files = read_of
                .iter()
                .map(|script| script["file"].as_str().unwrap().to_string());
            files.collect::<Vec<String>>()
        };
        assert_eq!(names("python3 run.py --all"), ["run.py"]);
        assert_eq!(
            names("bash tools/sync.sh && python3 run.py"),
            ["sync.sh", "run.py"]
        );
        // Twice named, read once; three named, two read.
        assert_eq!(names("python3 run.py run.py"), ["run.py"]);
        assert_eq!(
            names("cat tools/sync.sh run.py ./run.py x.py"),
            ["sync.sh", "run.py"]
        );
        // Not what is hidden, linked, too large, no text, not there, or reached by a way up.
        for command in [
            "python3 .hidden.py",
            "python3 link.py",
            "python3 big.py",
            "python3 binary.py",
            "python3 nothere.py",
            "python3 tools/../run.py",
        ] {
            assert_eq!(names(command), Vec::<String>::new(), "{command}");
        }
        let whole = scripts_read(
            &root,
            json!({ "command": "python3 run.py" }).as_object().unwrap(),
        );
        assert_eq!(
            whole,
            [
                json!({ "file": "run.py", "source_excerpt": "print('rows')\n", "excerpt_only": true })
            ]
        );
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn a_row_is_kept_under_a_name_of_its_signature_and_reader() {
        let (two, three) = (place("sh:git #abc", 2), place("sh:git #abc", 3));
        assert_eq!(two.len(), 40);
        assert_ne!(two, three);
        assert_eq!(two, place("sh:git #abc", 2));
    }
}
