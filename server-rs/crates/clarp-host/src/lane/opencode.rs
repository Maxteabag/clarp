//! The OpenCode driver of the lane layer (issue 763): one `opencode serve` kept per agent
//! between its chat turns, for agents of `opencode` and of `deepseek` (the same program and the
//! same session store), while the owner's `opencode_chat_lane` is on. What every lane shares is
//! in [`crate::lane`]; the lane process is the shim's `opencode-lane`.
//!
//! No turn waits for a lane that is not ready (the lead's rule for every driver): a turn with no
//! warm lane runs on its own server as before, and the lane starts beside it without the
//! session. That turn holds the session's lock shared for its life; the lane takes it exclusive
//! with its first claim, so the session is never written by two servers.

use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::time::Duration;

use clarp_shim::lane::{Claim, key};
use clarp_shim::opencode_lane::{Request, Snapshot};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use super::{Endpoint, Place, Settings, TURN_FILE_ENV, Tool};

/// How long a turn's look at the lane may take before the turn runs without it.
const LOOK: Duration = Duration::from_millis(500);
/// The largest program whose content is hashed into the digest: a wrapper that names the pin
/// (`exec mise x opencode@1.18.33 -- opencode`). A binary itself counts by its size and time.
const PROGRAM_HASHED_MOST: u64 = 1024 * 1024;
/// The most of a `.opencode` folder read for the digest; beyond it no lane is kept.
const FOLDER_MOST_BYTES: u64 = 4 * 1024 * 1024;
const FOLDER_MOST_FILES: usize = 500;

/// Where an OpenCode agent's sessions are locked, in the run folder (see
/// [`super::guard_exec`]: a turn on its own server holds the lock shared, a lane exclusive).
pub(crate) fn locks(run: &Path) -> PathBuf {
    run.join("opencode-sessions")
}

/// The program a lane runs: the Host's own resolved OpenCode (`CLARP_OPENCODE_PROGRAM`, the
/// owner's pin), made absolute against the Host's PATH, never looked up again by the lane. On
/// the owner's laptop `/usr/bin/opencode` is 2.x, which cannot read Clarp's 1.x sessions.
pub(crate) fn program(named: &str) -> Option<PathBuf> {
    let path = Path::new(named);
    if path.components().count() > 1 {
        return Some(path.to_owned());
    }
    std::env::split_paths(&std::env::var_os("PATH")?)
        .map(|dir| dir.join(named))
        .find(|candidate| candidate.is_file())
}

/// What a turn of an agent gives its lane: the words, the session, model and effort, as the
/// per-turn driver reads them from the turn's input.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Words {
    pub session: String,
    pub text: String,
    pub model: String,
    pub effort: String,
}

impl Words {
    /// The turn's input, written for `clarp-shim opencode-turn`.
    pub(crate) fn of(order: &clarp_actor::ShimOrder) -> io::Result<Self> {
        let input: Value =
            serde_json::from_slice(&std::fs::read(&order.stdin_file)?).map_err(io::Error::other)?;
        let field = |name: &str| input[name].as_str().unwrap_or_default().to_owned();
        Ok(Self {
            session: field("session"),
            text: field("prompt"),
            model: field("model"),
            effort: field("effort"),
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Lane {
    endpoint: Endpoint,
}

impl Lane {
    pub(crate) fn of(run: &Path, agent_id: &str) -> Self {
        Self {
            endpoint: Endpoint::of(Tool::OpenCode, run, agent_id),
        }
    }

    /// Whether this turn can run on the lane now, without waiting: a lane that answers within
    /// [`LOOK`], is free, was started as the turn would start one (`digest`), and holds the
    /// turn's session or none yet. Anything else, the turn runs on its own server.
    pub(crate) fn ready(&self, digest: &str, session: &str) -> bool {
        let Ok(state) = self.look() else {
            return false;
        };
        state.available
            && state.claim.is_none()
            && state.options == digest
            && (state.thread.is_empty() || state.thread == session)
    }

    /// Whether the lane holds `session` now (its lock with it).
    pub(crate) fn holds(&self, session: &str) -> bool {
        self.look().is_ok_and(|state| state.thread == session)
    }

    fn look(&self) -> io::Result<Snapshot> {
        let mut stream = UnixStream::connect(self.endpoint.socket())?;
        stream.set_read_timeout(Some(LOOK))?;
        stream.set_write_timeout(Some(LOOK))?;
        serde_json::to_writer(&mut stream, &Request::Inspect).map_err(io::Error::other)?;
        stream.write_all(b"\n")?;
        let mut line = Vec::new();
        BufReader::new(stream)
            .take(1024 * 1024)
            .read_until(b'\n', &mut line)?;
        let reply: Value = serde_json::from_slice(&line).map_err(io::Error::other)?;
        serde_json::from_value(reply["state"].clone()).map_err(io::Error::other)
    }

    /// Starts the agent's lane beside a turn of `session` that runs on its own server, with no
    /// session: the lane takes it with its first claim. A lane of the same options already there is kept;
    /// one started otherwise is retired while idle. Room is made under the Host's limits first.
    /// Blocking: waits for the new lane to say it is ready. `None`: a lane was there.
    pub(crate) fn start(
        &self,
        launcher: &crate::launcher::Launcher,
        profile: &clarp_actor::Profile,
        start: &Start,
        session: &str,
        want: super::Want,
    ) -> io::Result<Option<Child>> {
        if let Some(why) = clarp_spool::folder_gone(&profile.cwd) {
            return Err(io::Error::new(io::ErrorKind::NotFound, why));
        }
        let settings = Settings::read();
        // A lane that holds the turn's session still fits it; one that holds none fits any.
        // Either is kept when its options are the same; any other is retired while idle.
        if let Ok(state) = self.look()
            && state.options == start.digest
            && (state.thread.is_empty() || state.thread == session)
        {
            return Ok(None);
        }
        let place = self.endpoint.place(
            &profile.run_dir,
            Some(session),
            &start.digest,
            |_| false,
            settings.room(),
            // No turn needs this lane: a turn runs on its own server without one.
            want == super::Want::Needed,
        )?;
        // Kept: the lane that runs is the one wanted. Full: every place runs work, so none
        // starts, and the turns run on their own servers until a place is free (#786).
        if matches!(place, Place::Kept | Place::Full) {
            return Ok(None);
        }
        let options = self.options(profile, start, settings.idle_ms)?;
        let file = profile
            .dir
            .join(format!("opencode-lane-{}.json", crate::ids::hex(8)));
        let result = self.spawn(launcher, profile, start, &options, &file);
        let _ = std::fs::remove_file(file);
        result
    }

    fn options(
        &self,
        profile: &clarp_actor::Profile,
        start: &Start,
        idle_ms: u64,
    ) -> io::Result<Value> {
        let locks = locks(&profile.run_dir);
        std::fs::create_dir_all(&locks)?;
        Ok(json!({
            "cwd": profile.cwd,
            key::THREAD_LOCK_DIR: locks,
            key::LOGIN_FILE: start.login_file,
            key::IDLE_MS: idle_ms,
            key::TURN_FILE: self.endpoint.turn_file(&profile.run_dir),
            key::OPTIONS_DIGEST: start.digest,
        }))
    }

    fn spawn(
        &self,
        launcher: &crate::launcher::Launcher,
        profile: &clarp_actor::Profile,
        start: &Start,
        options: &Value,
        file: &Path,
    ) -> io::Result<Option<Child>> {
        let written = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(file)?;
        serde_json::to_writer(written, options).map_err(io::Error::other)?;
        let unit = format!("clarp-opencode-lane-{}", crate::ids::hex(4));
        let mut command = launcher.lane_command(
            &unit,
            "opencode-lane",
            self.endpoint.socket(),
            &start.program.to_string_lossy(),
            file,
            &profile.cwd,
        );
        command
            .envs(start.env.iter().map(|(k, v)| (k.as_str(), v.as_str())))
            .env_remove(crate::turn_identity::TURN_ENV)
            .env(TURN_FILE_ENV, self.endpoint.turn_file(&profile.run_dir));
        let mut child = command.spawn()?;
        let mut ready = String::new();
        if let Some(stdout) = child.stdout.take() {
            BufReader::new(stdout).read_line(&mut ready)?;
        }
        if ready.trim() == "ready" {
            Ok(Some(child))
        } else {
            let _ = child.wait();
            Err(io::Error::other("the OpenCode lane did not start"))
        }
    }

    /// Gives the turn to the lane. `token` is the turn's identity for the Clarp tools it calls.
    /// A refusal before the turn's spool is made reached nothing of OpenCode.
    pub(crate) fn submit(
        &self,
        order: &clarp_actor::ShimOrder,
        words: Words,
        token: Option<String>,
    ) -> io::Result<Snapshot> {
        self.endpoint.ask(&Request::Prompt {
            claim: Claim {
                turn: order.turn.0,
                attempt: order.attempt,
                spool: order.spool.clone(),
                socket: order.socket.clone(),
            },
            session: words.session,
            text: words.text,
            model: words.model,
            effort: words.effort,
            token,
        })
    }

    pub(crate) fn retire(&self) -> io::Result<()> {
        self.endpoint.retire()
    }
}

/// What a lane is started with, and the digest of all of it that a running lane cannot change:
/// computed at each turn's start, so that a change reaches the next turn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Start {
    pub program: PathBuf,
    /// The environment the server and everything it starts inherit: the turn's, without the
    /// turn's identity (a lane's tools read it from the turn file).
    pub env: Vec<(String, String)>,
    pub login_file: Option<PathBuf>,
    pub digest: String,
}

impl Start {
    /// `env` is the turn's (the order's). `None`: no lane can be kept for this turn (no program,
    /// or a configuration folder too large to read at every turn).
    pub(crate) fn of(
        named: &str,
        cwd: &Path,
        env: &[(String, String)],
        config_home: &Path,
        data_home: &Path,
    ) -> Option<Self> {
        let program = program(named)?;
        let mut env: Vec<(String, String)> = env
            .iter()
            .filter(|(key, _)| key != crate::turn_identity::TURN_ENV && key != TURN_FILE_ENV)
            .cloned()
            .collect();
        env.sort();
        env.dedup_by(|a, b| a.0 == b.0);
        let login_file = data_home.join("opencode").join("auth.json");
        let mut hash = Sha256::new();
        let mut put = |label: &str, bytes: &[u8]| {
            hash.update((label.len() as u64).to_le_bytes());
            hash.update(label.as_bytes());
            hash.update((bytes.len() as u64).to_le_bytes());
            hash.update(bytes);
        };
        put("program", program.to_string_lossy().as_bytes());
        put("program-content", &program_identity(&program));
        put("cwd", cwd.to_string_lossy().as_bytes());
        for (key, value) in &env {
            put(key.as_str(), value.as_bytes());
        }
        for path in config_files(cwd, config_home, &env, &login_file) {
            put(
                path.to_string_lossy().as_ref(),
                &std::fs::read(&path).unwrap_or_default(),
            );
        }
        for folder in project_folders(cwd) {
            hash_folder(&folder, &mut put)?;
        }
        Some(Self {
            program,
            env,
            login_file: Some(login_file),
            digest: format!("{:x}", hash.finalize()),
        })
    }
}

/// The program's content where it is a small wrapper (the pin is in its words), else its size
/// and modification time: hashing a 200 MB binary at every turn costs more than it tells.
fn program_identity(program: &Path) -> Vec<u8> {
    let Ok(meta) = std::fs::metadata(program) else {
        return Vec::new();
    };
    if meta.len() <= PROGRAM_HASHED_MOST {
        return std::fs::read(program).unwrap_or_default();
    }
    let modified = meta
        .modified()
        .ok()
        .and_then(|at| at.duration_since(std::time::UNIX_EPOCH).ok())
        .map_or(0, |at| at.as_nanos());
    format!("{}:{modified}", meta.len()).into_bytes()
}

/// The folders from the agent's to its worktree's root (where a `.git` is), the agent's first:
/// where OpenCode looks for a project's `opencode.json` and `.opencode`.
fn project_folders(cwd: &Path) -> Vec<PathBuf> {
    let mut folders = Vec::new();
    for dir in cwd.ancestors() {
        folders.push(dir.to_owned());
        if dir.join(".git").exists() {
            break;
        }
    }
    if folders
        .last()
        .is_some_and(|last| !last.join(".git").exists())
    {
        // No worktree above the agent's folder: only its own.
        folders.truncate(1);
    }
    folders
}

/// The files whose content makes a kept server stale: the project's and the global
/// configuration, files the environment names for it, and the login file (the lead's rule of
/// 24 Nov 2026: a content hash, never a size or a time).
fn config_files(
    cwd: &Path,
    config_home: &Path,
    env: &[(String, String)],
    login_file: &Path,
) -> Vec<PathBuf> {
    let mut files = Vec::new();
    for dir in project_folders(cwd) {
        files.push(dir.join("opencode.json"));
        files.push(dir.join("opencode.jsonc"));
    }
    let global = config_home.join("opencode");
    for name in [
        "opencode.json",
        "opencode.jsonc",
        "config.json",
        "AGENTS.md",
        "package.json",
    ] {
        files.push(global.join(name));
    }
    for (key, value) in env {
        if key == "OPENCODE_CONFIG" {
            files.push(PathBuf::from(value));
        }
    }
    files.push(login_file.to_owned());
    files
}

/// A project's `.opencode` folder (agents, commands, plugins) and one an `OPENCODE_CONFIG_DIR`
/// names: names and contents, `node_modules` left out. `None` past the folder limits.
fn hash_folder(dir: &Path, put: &mut impl FnMut(&str, &[u8])) -> Option<()> {
    let root = dir.join(".opencode");
    let mut stack = vec![root];
    let (mut files, mut bytes) = (0usize, 0u64);
    let mut found = Vec::new();
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if entry.file_name() == "node_modules" {
                continue;
            }
            let Ok(kind) = entry.file_type() else {
                continue;
            };
            if kind.is_dir() {
                stack.push(path);
            } else if kind.is_file() {
                files += 1;
                bytes += entry.metadata().map_or(0, |meta| meta.len());
                if files > FOLDER_MOST_FILES || bytes > FOLDER_MOST_BYTES {
                    return None;
                }
                found.push(path);
            }
        }
    }
    found.sort();
    for path in found {
        put(
            path.to_string_lossy().as_ref(),
            &std::fs::read(&path).unwrap_or_default(),
        );
    }
    Some(())
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "opencode-lane-{name}-{}-{}",
            std::process::id(),
            crate::ids::hex(4)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn the_digest_follows_content_never_a_files_time() {
        let root = scratch("digest");
        let (work, config, data) = (root.join("work"), root.join("config"), root.join("data"));
        for dir in [&work, &config.join("opencode"), &data.join("opencode")] {
            std::fs::create_dir_all(dir).unwrap();
        }
        let program = root.join("opencode");
        std::fs::write(
            &program,
            "exec mise x opencode@1.18.33 -- opencode \"$@\"\n",
        )
        .unwrap();
        let env = vec![("OPENCODE_PERMISSION".to_string(), "{}".to_string())];
        let digest = || {
            Start::of(&program.to_string_lossy(), &work, &env, &config, &data)
                .unwrap()
                .digest
        };
        let first = digest();
        assert_eq!(first, digest());
        // The agent's own configuration, the global one and the login each move it.
        let agent = work.join("opencode.json");
        std::fs::write(&agent, r#"{"provider":{}}"#).unwrap();
        let with_agent = digest();
        assert_ne!(with_agent, first);
        // Written again with the same words (a new time): the same lane.
        std::fs::write(&agent, r#"{"provider":{}}"#).unwrap();
        assert_eq!(digest(), with_agent);
        std::fs::write(config.join("opencode/opencode.json"), "{}").unwrap();
        let with_global = digest();
        assert_ne!(with_global, with_agent);
        std::fs::write(data.join("opencode/auth.json"), r#"{"a":{"type":"api"}}"#).unwrap();
        let with_login = digest();
        assert_ne!(with_login, with_global);
        // The pin is in the wrapper's words.
        std::fs::write(&program, "exec mise x opencode@1.19.0 -- opencode \"$@\"\n").unwrap();
        assert_ne!(digest(), with_login);
        // The turn's identity is no part of what the server is started with.
        let with_turn = {
            let mut env = env.clone();
            env.push((crate::turn_identity::TURN_ENV.to_string(), "t".to_string()));
            Start::of(&program.to_string_lossy(), &work, &env, &config, &data).unwrap()
        };
        assert_eq!(with_turn.digest, digest());
        assert!(
            with_turn
                .env
                .iter()
                .all(|(key, _)| key != crate::turn_identity::TURN_ENV)
        );
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn the_program_is_the_one_named_never_another_on_the_path() {
        assert_eq!(
            program("/home/a/.local/bin/opencode"),
            Some(PathBuf::from("/home/a/.local/bin/opencode"))
        );
        assert_eq!(program("no-such-opencode-program-anywhere"), None);
    }
}
