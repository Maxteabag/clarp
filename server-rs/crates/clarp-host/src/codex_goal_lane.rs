//! The connection from an agent worker to its restartable Codex lane: the one app-server that
//! runs the agent's chat turns while the owner's setting is on (issue 742), and a native
//! goal's turns always.

use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::time::Duration;

use clarp_shim::codex_lane::{Claim, GoalAction, LOGIN_CHANGED, Request, Snapshot};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

const MAX_STATE: u64 = 8 * 1024 * 1024;
// A first mutation may wait for thread/resume and its own result.
const RPC_TIMEOUT: Duration = Duration::from_secs(30);
/// What a lane's tools find the identity of its running turn in, in the lane's environment
/// only. A tool reads it where `CLARP_PROVIDER_TURN` is not set.
pub(crate) const TURN_FILE_ENV: &str = "CLARP_PROVIDER_TURN_FILE";

/// The owner's settings of Codex lanes, in this Host's `config.toml`, read anew at each use so
/// they change without a build or a restart. Top-level keys, before any table:
///
/// ```toml
/// codex_chat_lane = true        # Codex chat turns run on the agent's lane (default false)
/// codex_lane_idle_minutes = 15  # a lane with no turn and no goal leaves after this long
/// codex_lane_most = 8           # live lanes this Host keeps; the longest idle one leaves first
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Settings {
    pub chat: bool,
    pub idle_ms: u64,
    pub most: usize,
}

impl Settings {
    pub(crate) fn read() -> Self {
        let path = crate::config::host_config_dir().join("config.toml");
        Self::of(crate::owner::OwnerFile::new(Some(path)).read().as_ref())
    }

    fn of(file: Option<&toml::Table>) -> Self {
        let number = |key: &str, default: i64| {
            file.and_then(|file| file.get(key))
                .and_then(toml::Value::as_integer)
                .filter(|n| *n > 0)
                .unwrap_or(default)
        };
        Self {
            chat: file
                .and_then(|file| file.get("codex_chat_lane"))
                .and_then(toml::Value::as_bool)
                .unwrap_or(false),
            idle_ms: u64::try_from(number("codex_lane_idle_minutes", 15))
                .unwrap_or(15)
                .saturating_mul(60_000),
            most: usize::try_from(number("codex_lane_most", 8)).unwrap_or(8),
        }
    }
}

fn thread_lock(run: &Path, thread: &str) -> io::Result<PathBuf> {
    let locks = run.join("codex-threads");
    std::fs::create_dir_all(&locks)?;
    Ok(clarp_shim::codex_lane::thread_lock(&locks, thread))
}

/// Where an agent's lane keeps the identity of its running turn.
pub(crate) fn turn_file(run: &Path, agent_id: &str) -> PathBuf {
    run.join(format!("{agent_id}-turn.token"))
}

/// Regular exec keeps a read lease. A lane requires the exclusive lease and
/// holds it until its app-server has exited, even when another agent has
/// taken up the same native thread.
pub(crate) fn guard_exec(
    order: &mut clarp_actor::ShimOrder,
    run: &Path,
    thread: &str,
) -> io::Result<()> {
    let lock = thread_lock(run, thread)?;
    let program = std::mem::replace(&mut order.program, "flock".into());
    let args = std::mem::take(&mut order.args);
    order.args = vec![
        "--shared".into(),
        "--nonblock".into(),
        "--no-fork".into(),
        lock.to_string_lossy().into_owned(),
        program,
    ];
    order.args.extend(args);
    Ok(())
}

#[derive(Clone)]
pub(crate) enum Submission {
    Prompt {
        model: Option<String>,
        effort: Option<String>,
    },
    Activate {
        action: GoalAction,
        objective: String,
    },
    Recover,
}

pub(crate) struct Login {
    pub lock: Option<PathBuf>,
    pub file: Option<PathBuf>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Lane {
    socket: PathBuf,
}

impl Lane {
    pub(crate) fn new(socket: PathBuf) -> Self {
        Self { socket }
    }

    pub(crate) fn socket(&self) -> &Path {
        &self.socket
    }

    /// Attach to the current owner or return a new child to its reaper. `thread` is `None`
    /// for a new conversation: the lane starts the thread with its first prompt. `turn` is the
    /// model and effort a prompt asks for. A lane that was started otherwise (another thread,
    /// other options, a model or effort Codex would keep from an earlier turn) is retired while
    /// idle and started again; one that still runs work is used as it is, for its own thread.
    pub(crate) fn start(
        &self,
        launcher: &crate::launcher::Launcher,
        profile: &clarp_actor::Profile,
        login: &Login,
        thread: Option<&str>,
        turn: Option<(&Option<String>, &Option<String>)>,
    ) -> io::Result<Option<Child>> {
        let settings = Settings::read();
        let options = self.options(profile, login, thread, settings.idle_ms)?;
        let digest = options["_clarpOptionsDigest"]
            .as_str()
            .unwrap_or_default()
            .to_owned();
        if let Ok(state) = self.ask(Request::Inspect) {
            let same_thread = state.thread == thread.unwrap_or_default();
            let kept = turn.is_some_and(|(model, effort)| {
                (model.is_none() && state.model.is_some())
                    || (effort.is_none() && state.effort.is_some())
            });
            if same_thread && state.options == digest && !kept {
                return Ok(None);
            }
            if self.retire().is_err() {
                return if same_thread {
                    Ok(None)
                } else {
                    Err(io::Error::other("the Codex lane owns another thread"))
                };
            }
        }
        if !self.released()? {
            return Err(io::Error::other(
                "the Codex lane owner has not disconnected",
            ));
        }
        std::fs::create_dir_all(&profile.run_dir)?;
        make_room(&profile.run_dir, &self.socket, settings.most);
        let config = profile
            .dir
            .join(format!("native-goal-{}.json", crate::ids::hex(8)));
        let result = self.spawn(launcher, profile, &options, &config);
        let _ = std::fs::remove_file(config);
        result
    }

    /// What Codex is started with, and the lane's own settings. The digest covers what makes
    /// one lane differ from another for the same thread.
    fn options(
        &self,
        profile: &clarp_actor::Profile,
        login: &Login,
        thread: Option<&str>,
        idle_ms: u64,
    ) -> io::Result<Value> {
        let mut resume = json!({
            "cwd": profile.cwd,
            "excludeTurns": true,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            // What a turn's commands are given whatever Codex's own policy passes on: who
            // the agent is, and where its running turn's identity is.
            "config": {"shell_environment_policy": {"set": {
                "CLAUDE_PWA_SESSION": profile.session,
                "CLARP_SESSION": profile.session,
                TURN_FILE_ENV: self.turn_file(profile),
            }}},
        });
        if let Some(identity) = profile
            .told
            .as_ref()
            .and_then(|told| told.identity.as_ref())
        {
            resume["developerInstructions"] = json!(identity);
        }
        if let Some(model) = &profile.model {
            resume["model"] = json!(model);
        }
        let digest = format!("{:x}", Sha256::digest(resume.to_string().as_bytes()));
        let locks = profile.run_dir.join("codex-threads");
        std::fs::create_dir_all(&locks)?;
        if let Some(thread) = thread {
            resume["threadId"] = json!(thread);
        }
        resume["_clarpThreadLockDir"] = json!(locks);
        resume["_clarpLoginLock"] = json!(login.lock);
        resume["_clarpLoginFile"] = json!(login.file);
        resume["_clarpIdleMs"] = json!(idle_ms);
        resume["_clarpTurnFile"] = json!(self.turn_file(profile));
        resume["_clarpOptionsDigest"] = json!(digest);
        Ok(resume)
    }

    fn turn_file(&self, profile: &clarp_actor::Profile) -> PathBuf {
        let agent = self
            .socket
            .file_name()
            .and_then(|name| name.to_str())
            .and_then(|name| name.strip_suffix("-cxg.sock"))
            .unwrap_or("lane");
        turn_file(&profile.run_dir, agent)
    }

    fn spawn(
        &self,
        launcher: &crate::launcher::Launcher,
        profile: &clarp_actor::Profile,
        resume: &Value,
        config: &Path,
    ) -> io::Result<Option<Child>> {
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(config)?;
        serde_json::to_writer(file, resume).map_err(io::Error::other)?;
        let program = clarp_backend::program("codex")
            .ok_or_else(|| io::Error::other("Codex is not configured"))?;
        let mut child = launcher
            .goal_lane_command(self.socket(), &program, config, &profile.cwd)
            .envs(
                profile
                    .tool_env
                    .iter()
                    .map(|(key, value)| (key.as_str(), value.as_str())),
            )
            .env_remove("CLARP_PROVIDER_TURN")
            .env(TURN_FILE_ENV, self.turn_file(profile))
            .spawn()?;
        let mut ready = String::new();
        if let Some(stdout) = child.stdout.take() {
            BufReader::new(stdout).read_line(&mut ready)?;
        }
        if ready.trim() == "ready" {
            Ok(Some(child))
        } else {
            let _ = child.wait();
            Err(io::Error::other("the Codex lane did not start"))
        }
    }

    /// The actor and login pool have both admitted this attempt. `token` is the turn's
    /// identity for the Clarp tools it calls. A refusal for a changed login has the kind
    /// `PermissionDenied`: the lane has left, and nothing of the attempt reached Codex.
    pub(crate) fn submit(
        &self,
        order: clarp_actor::ShimOrder,
        submission: Submission,
        token: Option<String>,
    ) -> io::Result<Snapshot> {
        let claim = Claim {
            turn: order.turn.0,
            attempt: order.attempt,
            spool: order.spool,
            socket: order.socket,
        };
        match submission {
            Submission::Prompt { model, effort } => {
                let text = std::fs::read_to_string(order.stdin_file)?;
                self.ask(Request::Prompt {
                    claim,
                    text,
                    model,
                    effort,
                    token,
                })
            }
            Submission::Activate { action, objective } => {
                self.ask(Request::Reserve { claim, token })?;
                self.ask(Request::Goal { action, objective })
            }
            Submission::Recover => {
                self.ask(Request::Reserve { claim, token })?;
                self.ask(Request::Recover)
            }
        }
    }

    pub(crate) fn recover_stop(&self) -> io::Result<Snapshot> {
        let observed = self.ask(Request::Probe)?;
        if observed
            .goal
            .as_ref()
            .is_some_and(|goal| goal["status"] == "active")
        {
            self.ask(Request::Goal {
                action: GoalAction::Pause,
                objective: String::new(),
            })
        } else {
            Ok(observed)
        }
    }

    pub(crate) fn ask(&self, request: Request) -> io::Result<Snapshot> {
        let stream = self.connect(&request)?;
        replied(&mut BufReader::new(stream))
    }

    /// Losing a watcher says nothing about thread ownership. The agent must
    /// reconcile it with the lane lock/claim before choosing another launcher.
    pub(crate) fn watch(&self, mut observe: impl FnMut(Snapshot) -> bool) -> io::Result<()> {
        let stream = self.connect(&Request::Watch)?;
        let mut input = BufReader::new(stream);
        if !observe(replied(&mut input)?) {
            return Ok(());
        }
        input.get_ref().set_read_timeout(None)?;
        loop {
            let state = serde_json::from_slice(&line(&mut input)?).map_err(io::Error::other)?;
            if !observe(state) {
                return Ok(());
            }
        }
    }

    /// Ask an idle lane to leave, and wait until it has: its owner lock is let go only after
    /// its app-server has been reaped. A lane that still runs work refuses.
    pub(crate) fn retire(&self) -> io::Result<()> {
        if self.released()? {
            return Ok(());
        }
        self.ask(Request::Retire)?;
        self.wait_released()
    }

    /// Waits for the lane's owner lock, which its process holds until it is gone.
    pub(crate) fn wait_released(&self) -> io::Result<()> {
        let file = match OpenOptions::new()
            .read(true)
            .write(true)
            .open(self.socket.with_extension("lock"))
        {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error),
        };
        file.lock()
    }

    /// A retirement response only acknowledges the request. The process
    /// releases the owner lock after reaping the app-server child.
    pub(crate) fn released(&self) -> io::Result<bool> {
        let file = match OpenOptions::new()
            .read(true)
            .write(true)
            .open(self.socket.with_extension("lock"))
        {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(true),
            Err(error) => return Err(error),
        };
        match file.try_lock() {
            Ok(()) => Ok(true),
            Err(std::fs::TryLockError::WouldBlock) => Ok(false),
            Err(std::fs::TryLockError::Error(error)) => Err(error),
        }
    }

    fn connect(&self, request: &Request) -> io::Result<UnixStream> {
        let mut stream = UnixStream::connect(&self.socket)?;
        stream.set_read_timeout(Some(RPC_TIMEOUT))?;
        stream.set_write_timeout(Some(RPC_TIMEOUT))?;
        serde_json::to_writer(&mut stream, request).map_err(io::Error::other)?;
        stream.write_all(b"\n")?;
        Ok(stream)
    }
}

/// Keeps at most `most` lanes alive on this Host, this one about to start included: the idle
/// lanes that held no turn the longest are asked to leave first. A lane that runs a turn, or
/// whose goal may continue, refuses and counts. Read only when a lane is about to start.
fn make_room(run: &Path, own: &Path, most: usize) {
    let Ok(entries) = std::fs::read_dir(run) else {
        return;
    };
    let mut idle = Vec::new();
    let mut live = 0usize;
    for entry in entries.flatten() {
        let path = entry.path();
        let is_lane = path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.ends_with("-cxg.sock"));
        if !is_lane || path == own {
            continue;
        }
        let lane = Lane::new(path);
        if lane.released().unwrap_or(true) {
            continue;
        }
        live += 1;
        if let Ok(state) = lane.ask(Request::Inspect)
            && state.claim.is_none()
            && state.available
        {
            idle.push((state.idle_ms, lane));
        }
    }
    idle.sort_by_key(|(idle_ms, _)| std::cmp::Reverse(*idle_ms));
    for (_, lane) in idle {
        if live < most {
            break;
        }
        if lane.ask(Request::Retire).is_ok() {
            live -= 1;
        }
    }
}

#[derive(Deserialize)]
struct Reply {
    state: Option<Snapshot>,
    error: Option<String>,
    #[serde(default)]
    code: Option<String>,
}

fn replied(input: &mut impl BufRead) -> io::Result<Snapshot> {
    let reply: Reply = serde_json::from_slice(&line(input)?).map_err(io::Error::other)?;
    let kind = if reply.code.as_deref() == Some(LOGIN_CHANGED) {
        io::ErrorKind::PermissionDenied
    } else {
        io::ErrorKind::Other
    };
    reply.state.ok_or_else(|| {
        io::Error::new(
            kind,
            reply
                .error
                .unwrap_or_else(|| "Codex lane supplied no state".into()),
        )
    })
}

fn line(input: &mut impl BufRead) -> io::Result<Vec<u8>> {
    let mut line = Vec::new();
    input.take(MAX_STATE).read_until(b'\n', &mut line)?;
    if line.last() != Some(&b'\n') {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "Codex lane connection closed",
        ));
    }
    Ok(line)
}
