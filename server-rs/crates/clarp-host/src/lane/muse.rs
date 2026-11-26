//! The Muse driver of the lane layer (issue 762): one `muse serve` kept for an agent's chat
//! turns while the owner's `muse_chat_lane` is on. What every lane shares is in [`crate::lane`];
//! the lane's process is the shim's `muse-lane`.
//!
//! The driver never opens a conversation: Muse's `session/start` names only UUIDv7 ids, and the
//! Host's conversation ids are UUIDv4, so a conversation's first turn runs on exec, which names
//! the session. No turn waits for a cold lane: a turn with no lane ready runs on exec, and the
//! lane starts beside it without the session ([`Lane::warm`]); it takes the session once that
//! turn's process is gone ([`Lane::take`]). Muse refuses a second process on a session it holds,
//! so a lane that holds the session is retired, and its process waited out, before any exec
//! runs that session (the agent's `make_way`, within its bound).

use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::Child;

use clarp_shim::lane::key;
use clarp_shim::muse_lane::{Claim, Request, Snapshot, key as muse_key};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use super::{Endpoint, Place, Settings, TURN_FILE_ENV, Tool};

/// `muse serve`'s form of exec's `--disable-reminders` (measured on Muse 1.4.25: no reminder item,
/// no `turn/foregroundCompleted`). Muse's reminder agents are off on the lane as on exec: the
/// owner's answer on decision-d4c9cad82138ed99698f3691 was "Muse reminders off now, a
/// kept-running lane later", and stands until the lane's switch-on card asks him again (with them
/// on, they held each turn's end 1.7-3.4 s). It is in the options, so in the digest: a lane
/// started with them on is replaced at its next turn.
const WITHOUT_REMINDERS: &str = "run.reminder_roster=[]";

/// How long the Host waits for a starting lane to say it is ready: its `initialize` has 25 s in
/// the lane, and `muse` may update itself first. The wait is on the lane's own thread, never a
/// turn's.
const READY_WAIT: std::time::Duration = std::time::Duration::from_secs(40);

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Lane {
    endpoint: Endpoint,
}

impl Lane {
    /// The Muse lane of an agent, in its run folder.
    pub(crate) fn of(run: &Path, agent_id: &str) -> Self {
        Self {
            endpoint: Endpoint::of(Tool::Muse, run, agent_id),
        }
    }

    pub(crate) fn endpoint(&self) -> &Endpoint {
        &self.endpoint
    }

    pub(crate) fn ask(&self, request: &Request) -> io::Result<Snapshot> {
        self.endpoint.ask(request)
    }

    /// Whether the lane takes a turn of `session` that asks for `model` now: it runs, holds that
    /// session, was started with today's options, is free, and has given Muse no model the turn
    /// would not keep. Anything else and the turn runs on exec.
    pub(crate) fn ready(
        &self,
        profile: &clarp_actor::Profile,
        session: &str,
        model: Option<&str>,
    ) -> bool {
        let Ok(state) = self.ask(&Request::Inspect) else {
            return false;
        };
        let digest = digest(&options(profile, session, &self.endpoint, 0));
        state.thread == session
            && state.loaded
            && state.available
            && state.options == digest
            && !(model.is_none() && state.model.is_some())
    }

    /// Starts the lane for `session` beside a turn that runs on exec, without the session, or
    /// keeps the one that runs for it with the same options. Room is made first. Returns the new
    /// lane's process for its reaper.
    pub(crate) fn warm(
        &self,
        launcher: &crate::launcher::Launcher,
        profile: &clarp_actor::Profile,
        session: &str,
    ) -> io::Result<Option<Child>> {
        if let Some(why) = clarp_spool::folder_gone(&profile.cwd) {
            return Err(io::Error::new(io::ErrorKind::NotFound, why));
        }
        let settings = Settings::read();
        let mut options = options(profile, session, &self.endpoint, settings.idle_ms);
        let digest = digest(&options);
        // A lane started for another session or with other options leaves first, within the
        // bound, so `place` never waits on it unbounded; one that runs a turn refuses and stays.
        // A lane started otherwise is made to leave within the layer's bound (`LEAVE_WAIT`).
        let place = self.endpoint.place(
            &profile.run_dir,
            Some(session),
            &digest,
            |_| false,
            settings.room(),
            // Optional: no Muse goal needs a lane, and a chat turn runs per turn without one.
            false,
        )?;
        // Kept: the lane that runs is the one wanted. Full: every place runs work, so none starts;
        // nothing waits, the turn runs on exec, and the next warm start may try again (#786).
        let Place::Free(admission) = place else {
            return Ok(None);
        };
        options[key::OPTIONS_DIGEST] = json!(digest);
        let config = profile
            .dir
            .join(format!("muse-lane-{}.json", crate::ids::hex(8)));
        let result = self.spawn(launcher, profile, &options, &config);
        let _ = std::fs::remove_file(config);
        // Ready: it listens, and every later look sees it. A launch that failed gives its place
        // back as the admission is dropped.
        if matches!(result, Ok(Some(_))) {
            admission.visible();
        }
        result
    }

    /// The exec turn that held the session is over and its process gone: the lane takes it.
    pub(crate) fn take(&self) -> io::Result<Snapshot> {
        self.ask(&Request::Take)
    }

    /// The actor has admitted this attempt; the lane runs it. `token` is the turn's identity for
    /// the Clarp tools it calls.
    pub(crate) fn submit(
        &self,
        order: &clarp_actor::ShimOrder,
        token: Option<String>,
    ) -> io::Result<Snapshot> {
        let text = prompt(&order.args, order.prompt_as_file, &order.stdin_file)?;
        self.ask(&Request::Prompt {
            claim: Claim {
                turn: order.turn.0,
                attempt: order.attempt,
                spool: order.spool.clone(),
                socket: order.socket.clone(),
            },
            text,
            model: order.model.clone(),
            effort: order.effort.clone(),
            token,
        })
    }

    fn spawn(
        &self,
        launcher: &crate::launcher::Launcher,
        profile: &clarp_actor::Profile,
        options: &Value,
        config: &Path,
    ) -> io::Result<Option<Child>> {
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(config)?;
        serde_json::to_writer(file, options).map_err(io::Error::other)?;
        let program = clarp_backend::program("muse")
            .ok_or_else(|| io::Error::other("Muse is not configured"))?;
        let unit = format!("clarp-muse-lane-{}", crate::ids::hex(4));
        let mut command = launcher.lane_command(
            &unit,
            "muse-lane",
            self.endpoint.socket(),
            &program,
            config,
            &profile.cwd,
        );
        command
            .envs(
                profile
                    .tool_env
                    .iter()
                    .map(|(key, value)| (key.as_str(), value.as_str())),
            )
            .env(clarp_backend::SESSION_ENV, &profile.session)
            .env_remove("CLARP_PROVIDER_TURN")
            .env(TURN_FILE_ENV, self.endpoint.turn_file(&profile.run_dir));
        let mut child = command.spawn()?;
        // Its first line, read on a thread of its own so that the wait has a bound: a lane that
        // says nothing by then is ended (`muse` updates itself as it starts, and Meta's provider
        // starts before `initialize` answers).
        let (said, ready) = std::sync::mpsc::sync_channel(1);
        if let Some(stdout) = child.stdout.take() {
            let _ = std::thread::Builder::new()
                .name("muse-lane-ready".into())
                .spawn(move || {
                    let mut line = String::new();
                    let _ = BufReader::new(stdout).read_line(&mut line);
                    let _ = said.send(line);
                });
        }
        match ready.recv_timeout(READY_WAIT) {
            Ok(line) if line.trim() == "ready" => Ok(Some(child)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                Err(io::Error::other(format!(
                    "the Muse lane did not start within {} s",
                    READY_WAIT.as_secs()
                )))
            }
        }
    }
}

/// The words exec would be given for the turn: its last argument, or its prompt file when it
/// was too long for a command line.
fn prompt(args: &[String], as_file: bool, stdin_file: &Path) -> io::Result<String> {
    if as_file {
        return std::fs::read_to_string(stdin_file);
    }
    let mut args = args.iter().rev();
    match (args.next(), args.next()) {
        (Some(text), Some(flag)) if flag == "--" => Ok(text.clone()),
        _ => Err(io::Error::other("the Muse turn names no prompt")),
    }
}

/// What the lane is started with: Muse's session and folder, the model the agent names, the one
/// reminder switch, and the lane's own keys. `idle_ms` is not part of what makes lanes differ.
fn options(
    profile: &clarp_actor::Profile,
    session: &str,
    endpoint: &Endpoint,
    idle_ms: u64,
) -> Value {
    let mut options = json!({
        muse_key::SESSION: session,
        muse_key::CWD: profile.cwd,
        muse_key::CONFIG: [WITHOUT_REMINDERS],
        key::TURN_FILE: endpoint.turn_file(&profile.run_dir),
        key::LOGIN_FILE: config_dir().join("auth.json"),
        key::IDLE_MS: idle_ms,
        "_clarpSession": profile.session,
        // What the lane's tools are given, as a digest only: it holds the agents' Host token,
        // which the options file (written to disk for the lane to read) must not.
        "_clarpToolEnv": format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&profile.tool_env).unwrap_or_default())
        ),
    });
    if let Some(model) = &profile.model {
        options[muse_key::MODEL] = json!(model);
    }
    options
}

/// What makes one lane differ from another for the same session: its options, and what Muse
/// reads only when it starts or loads the session: its binary (the `muse` wrapper updates
/// itself, and a kept serve would keep the old one) and its settings (provider, permissions,
/// MCP servers).
fn digest(options: &Value) -> String {
    let mut same = options.clone();
    if let Some(fields) = same.as_object_mut() {
        fields.remove(key::IDLE_MS);
        fields.remove(key::OPTIONS_DIGEST);
    }
    let program = clarp_backend::program("muse").and_then(|name| found(&name));
    let stamp = |path: Option<PathBuf>| {
        path.and_then(|path| std::fs::metadata(&path).ok())
            .map(|meta| format!("{:?}/{}", meta.modified().ok(), meta.len()))
            .unwrap_or_default()
    };
    let version = program
        .as_deref()
        .and_then(Path::parent)
        .and_then(|dir| std::fs::read_to_string(dir.join(".muse-version")).ok())
        .unwrap_or_default();
    format!(
        "{:x}",
        Sha256::digest(
            format!(
                "{same}|{}|{version}|{}",
                stamp(program),
                stamp(Some(config_dir().join("settings.json")))
            )
            .as_bytes()
        )
    )
}

/// Where Muse keeps its settings and login, as it finds them from the environment its turns
/// inherit.
fn config_dir() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .filter(|dir| !dir.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| crate::config::home().join(".config"))
        .join("muse")
}

/// The program's file: a path as given, or the first on `PATH`.
fn found(program: &str) -> Option<PathBuf> {
    if program.contains('/') {
        return Some(PathBuf::from(program));
    }
    std::env::split_paths(&std::env::var_os("PATH")?)
        .map(|dir| dir.join(program))
        .find(|path| path.is_file())
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn args(words: &[&str]) -> Vec<String> {
        words.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn the_lane_is_given_the_words_exec_would_be_given() {
        let none = Path::new("/nonexistent");
        let inline = args(&["exec", "--json", "--yolo", "--", "hello -- there"]);
        assert_eq!(prompt(&inline, false, none).unwrap(), "hello -- there");
        assert!(prompt(&args(&["exec", "--json"]), false, none).is_err());
        let file = std::env::temp_dir().join(format!("muse-prompt-{}", std::process::id()));
        std::fs::write(&file, "long words").unwrap();
        assert_eq!(
            prompt(&args(&["--prompt-file"]), true, &file).unwrap(),
            "long words"
        );
        std::fs::remove_file(file).unwrap();
    }

    #[test]
    fn a_lane_started_with_the_reminder_agents_differs_from_one_without() {
        let without = json!({"sessionId": "s", "config": [WITHOUT_REMINDERS]});
        let with = json!({"sessionId": "s"});
        assert_ne!(digest(&without), digest(&with));
    }

    #[test]
    fn the_idle_time_is_not_what_makes_a_lane_differ() {
        let options = json!({"sessionId": "s", "_clarpIdleMs": 1});
        let longer = json!({"sessionId": "s", "_clarpIdleMs": 2});
        let other = json!({"sessionId": "t", "_clarpIdleMs": 1});
        assert_eq!(digest(&options), digest(&longer));
        assert_ne!(digest(&options), digest(&other));
    }
}
