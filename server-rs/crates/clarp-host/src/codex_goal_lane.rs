//! The connection from an agent worker to its restartable native-goal lane.

use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::time::Duration;

use clarp_shim::codex_lane::{Claim, GoalAction, Request, Snapshot};
use serde::Deserialize;
use serde_json::json;
use sha2::{Digest, Sha256};

const MAX_STATE: u64 = 8 * 1024 * 1024;
// A first mutation may wait for thread/resume and its own result.
const RPC_TIMEOUT: Duration = Duration::from_secs(30);

fn thread_lock(run: &Path, thread: &str) -> io::Result<PathBuf> {
    let locks = run.join("codex-threads");
    std::fs::create_dir_all(&locks)?;
    Ok(locks.join(format!("{:x}.lock", Sha256::digest(thread.as_bytes()))))
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

    /// Attach to the current owner or return a new child to its reaper.
    pub(crate) fn start(
        &self,
        launcher: &crate::launcher::Launcher,
        profile: &clarp_actor::Profile,
        login: &Login,
        thread: &str,
    ) -> io::Result<Option<Child>> {
        if let Ok(state) = self.ask(Request::Inspect) {
            if state.thread == thread {
                return Ok(None);
            }
            return Err(io::Error::other("the Codex lane owns another thread"));
        }
        if !self.released()? {
            return Err(io::Error::other(
                "the Codex lane owner has not disconnected",
            ));
        }
        std::fs::create_dir_all(&profile.run_dir)?;
        let config = profile
            .dir
            .join(format!("native-goal-{}.json", crate::ids::hex(8)));
        let result = self.spawn(launcher, profile, login, thread, &config);
        let _ = std::fs::remove_file(config);
        result
    }

    fn spawn(
        &self,
        launcher: &crate::launcher::Launcher,
        profile: &clarp_actor::Profile,
        login: &Login,
        thread: &str,
        config: &Path,
    ) -> io::Result<Option<Child>> {
        let mut resume = json!({
            "threadId": thread,
            "cwd": profile.cwd,
            "excludeTurns": true,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "_clarpThreadLock": thread_lock(&profile.run_dir, thread)?,
            "_clarpLoginLock": login.lock,
            "_clarpLoginFile": login.file,
            "config": {"shell_environment_policy": {"set": {
                "CLAUDE_PWA_SESSION": profile.session,
                "CLARP_SESSION": profile.session,
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
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(config)?;
        serde_json::to_writer(file, &resume).map_err(io::Error::other)?;
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

    /// The actor and login pool have both admitted this attempt.
    pub(crate) fn submit(
        &self,
        order: clarp_actor::ShimOrder,
        submission: Submission,
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
                })
            }
            Submission::Activate { action, objective } => {
                self.ask(Request::Reserve { claim })?;
                self.ask(Request::Goal { action, objective })
            }
            Submission::Recover => {
                self.ask(Request::Reserve { claim })?;
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

#[derive(Deserialize)]
struct Reply {
    state: Option<Snapshot>,
    error: Option<String>,
}

fn replied(input: &mut impl BufRead) -> io::Result<Snapshot> {
    let reply: Reply = serde_json::from_slice(&line(input)?).map_err(io::Error::other)?;
    reply.state.ok_or_else(|| {
        io::Error::other(
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
