//! The Codex app-server driver of the lane layer: the connection from an agent worker to its
//! restartable Codex lane, the one app-server that runs the agent's chat turns while the
//! owner's setting is on (issue 742), and a native goal's turns always. What every lane shares
//! is in [`crate::lane`].

use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::Child;

use clarp_shim::codex_lane::{Claim, GoalAction, Request, Snapshot};
use clarp_shim::lane::key;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use super::{Endpoint, Place, Settings, TURN_FILE_ENV, Tool};

/// Where a Codex agent's threads are locked, in the run folder.
pub(crate) fn locks(run: &Path) -> PathBuf {
    run.join("codex-threads")
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
    /// The Codex home and provider of an agent bound to one of its own: its lane's Codex runs
    /// in that home and on that provider, and leaves when it names another.
    /// `Err`: the agent is bound, and its binding cannot be used: no lane starts for it.
    pub own: Result<Option<clarp_backend::own_provider::OwnProvider>, String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Lane {
    endpoint: Endpoint,
}

/// The binding a lane runs with, where it has a usable one: a lane whose binding cannot be
/// used never gets past [`Lane::start`].
fn bound(login: &Login) -> Option<&clarp_backend::own_provider::OwnProvider> {
    login.own.as_ref().ok().and_then(Option::as_ref)
}

impl Lane {
    /// The Codex lane of an agent, in its run folder.
    pub(crate) fn of(run: &Path, agent_id: &str) -> Self {
        Self {
            endpoint: Endpoint::of(Tool::Codex, run, agent_id),
        }
    }

    pub(crate) fn endpoint(&self) -> &Endpoint {
        &self.endpoint
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
        want: crate::lane::Want,
    ) -> io::Result<Option<Child>> {
        // A lane bound to a provider starts on that provider's configuration or not at all.
        crate::fleet::admitted(&login.own).map_err(io::Error::other)?;
        // Nor in a folder that is gone: the turn it was started for ends at once, as one.
        if let Some(why) = clarp_spool::folder_gone(&profile.cwd) {
            return Err(io::Error::new(io::ErrorKind::NotFound, why));
        }
        let settings = Settings::read();
        let options = self.options(profile, login, thread, settings.idle_ms)?;
        let digest = options[key::OPTIONS_DIGEST]
            .as_str()
            .unwrap_or_default()
            .to_owned();
        // Codex keeps a model or effort from an earlier turn: a turn that asks for the
        // defaults again needs a new lane.
        let kept = |state: &clarp_shim::lane::Status| {
            turn.is_some_and(|(model, effort)| {
                (model.is_none() && state.model.is_some())
                    || (effort.is_none() && state.effort.is_some())
            })
        };
        let place = self.endpoint.place(
            &profile.run_dir,
            thread,
            &digest,
            kept,
            settings.room(),
            want == crate::lane::Want::Needed,
        )?;
        match place {
            Place::Kept => return Ok(None),
            // Every place runs work and no goal needs this lane: the chat turn runs in a
            // process of its own instead (issue 786).
            Place::Full => return Err(crate::lane::NoPlace::error()),
            Place::Free => {}
        }
        let config = profile
            .dir
            .join(format!("native-goal-{}.json", crate::ids::hex(8)));
        let home = bound(login).map(|own| own.home.as_path());
        let result = self.spawn(launcher, profile, &options, &config, home);
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
        // An agent bound to a provider of its own names it on its thread, where neither a
        // profile nor a project can move it. Its home is part of what makes the lane its own.
        let mut own_home = String::new();
        if let Some(own) = bound(login) {
            resume["modelProvider"] = json!(own.provider);
            resume["config"]["model_provider"] = json!(own.provider);
            own_home = own.home.display().to_string();
        }
        let digest = format!(
            "{:x}",
            Sha256::digest(format!("{resume}{own_home}").as_bytes())
        );
        let locks = locks(&profile.run_dir);
        std::fs::create_dir_all(&locks)?;
        if let Some(thread) = thread {
            resume["threadId"] = json!(thread);
        }
        resume[key::THREAD_LOCK_DIR] = json!(locks);
        resume[key::LOGIN_LOCK] = json!(login.lock);
        resume[key::LOGIN_FILE] = json!(login.file);
        resume[key::IDLE_MS] = json!(idle_ms);
        resume[key::TURN_FILE] = json!(self.turn_file(profile));
        resume[key::OPTIONS_DIGEST] = json!(digest);
        if let Some(own) = bound(login) {
            resume[key::MODEL_PROVIDER] = json!(own.provider);
        }
        Ok(resume)
    }

    fn turn_file(&self, profile: &clarp_actor::Profile) -> PathBuf {
        self.endpoint.turn_file(&profile.run_dir)
    }

    fn spawn(
        &self,
        launcher: &crate::launcher::Launcher,
        profile: &clarp_actor::Profile,
        resume: &Value,
        config: &Path,
        home: Option<&Path>,
    ) -> io::Result<Option<Child>> {
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(config)?;
        serde_json::to_writer(file, resume).map_err(io::Error::other)?;
        let program = clarp_backend::program("codex")
            .ok_or_else(|| io::Error::other("Codex is not configured"))?;
        let mut command =
            launcher.goal_lane_command(self.endpoint.socket(), &program, config, &profile.cwd);
        command
            .envs(
                profile
                    .tool_env
                    .iter()
                    .map(|(key, value)| (key.as_str(), value.as_str())),
            )
            .env_remove("CLARP_PROVIDER_TURN")
            .env(TURN_FILE_ENV, self.turn_file(profile));
        // The lane's Codex runs in the agent's own home, for an agent bound to one.
        if let Some(home) = home {
            command.env(clarp_backend::codex::CODEX_HOME, home);
        }
        // A folder that went after `start` looked is said as the Host says it, so that the turn
        // ends at once and is not tried again as a launch that failed.
        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(error) => {
                return Err(match clarp_spool::folder_gone(&profile.cwd) {
                    Some(gone) => io::Error::new(io::ErrorKind::NotFound, gone),
                    None => error,
                });
            }
        };
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
    /// identity for the Clarp tools it calls. A refusal for a changed login is a
    /// [`crate::lane::LoginChanged`]: the lane has left, and nothing of the attempt reached Codex.
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
        self.endpoint.ask(&request)
    }

    /// Losing a watcher says nothing about thread ownership. The agent must
    /// reconcile it with the lane lock/claim before choosing another launcher.
    pub(crate) fn watch(&self, observe: impl FnMut(Snapshot) -> bool) -> io::Result<()> {
        self.endpoint.watch(&Request::Watch, observe)
    }

    /// Ask an idle lane to leave, and wait until it has.
    pub(crate) fn retire(&self) -> io::Result<()> {
        self.endpoint.retire()
    }

    pub(crate) fn released(&self) -> io::Result<bool> {
        self.endpoint.released()
    }
}
