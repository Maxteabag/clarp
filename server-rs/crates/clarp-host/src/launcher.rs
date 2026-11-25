//! Starting shims, following them, and telling them to stop.
//!
//! A shim is started by this process and is not this process's: it gets a session of its own,
//! and under systemd a scope of its own, so that stopping or restarting the Host leaves it and
//! its tool running. Everything here blocks, and runs on a thread that belongs to one turn.

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use clarp_actor::ShimOrder;
use clarp_kernel::steering::REPLAY_USER_MESSAGES;
use clarp_kernel::turn::TurnId;
use clarp_shim::{EXIT_NOT_STARTED, EXIT_TURN_TAKEN, Reply, Request};

use crate::ids;

/// After a hint, how long further hints are collected before the agent is told again. A tool
/// that prints fast is read in strides, not line by line.
const STRIDE: Duration = Duration::from_millis(25);

/// How long a shim is given to answer a request to stop its tool.
const STOP_TIMEOUT: Duration = Duration::from_secs(5);

/// How a shim is started.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Launcher {
    /// As a process of its own session. Enough wherever nothing kills by control group.
    Direct { shim: PathBuf },
    /// In a systemd scope of its own, so that restarting the Host's unit does not reach it.
    SystemdScope { shim: PathBuf },
}

/// What became of a launch.
#[derive(Debug)]
pub enum Launched {
    /// The shim has the turn and its tool is up. The child is kept only to be reaped.
    Ready(Child),
    /// Another shim has this turn. It is the one to follow.
    TurnTaken,
    /// The shim has the turn and could not start the tool. The spool says why.
    SaidWhy,
    /// No shim has the turn.
    Failed(String),
    NoShim(String),
    /// The turn was stopped before its shim was started: nothing was started for it.
    Stopped,
}

/// What a turn's thread tells the agent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Told {
    /// The turn's spool may have grown.
    SpoolChanged,
    /// Nothing answers at the turn's shim any more.
    Gone,
    /// No shim could be started for the turn.
    Failed(String),
    /// No shim could be started for a call's opening, in this many tries each a growing pause
    /// apart: the turn ends, and its caller is told why (issue 769).
    OpeningGaveUp { tries: u32, reason: String },
}

impl Launcher {
    /// The launcher with this name in configuration.
    pub fn by_name(name: &str, shim: PathBuf) -> Option<Self> {
        match name {
            "direct" => Some(Self::Direct { shim }),
            "systemd" => Some(Self::SystemdScope { shim }),
            _ => None,
        }
    }

    /// The CLI combines this invocation's hook with its usual settings.
    /// Omit it when the configured executable has no usable absolute name.
    pub fn claude_settings(&self, agent: &str) -> Option<String> {
        let (Self::Direct { shim } | Self::SystemdScope { shim }) = self;
        let path = if shim.components().count() == 1 && shim.is_relative() {
            std::env::split_paths(&std::env::var_os("PATH")?)
                .map(|p| p.join(shim))
                .find(|p| p.is_file())?
        } else {
            shim.clone()
        };
        let absolute = std::fs::canonicalize(path).ok()?;
        let command = format!(
            "{} claude-background-hook {}",
            clarp_kernel::python::shell_quote(absolute.to_str()?),
            clarp_kernel::python::shell_quote(agent)
        );
        Some(serde_json::json!({"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":command,"timeout":3}]}]}}).to_string())
    }

    /// The shim this launcher starts.
    pub(crate) fn shim(&self) -> &Path {
        match self {
            Self::Direct { shim } | Self::SystemdScope { shim } => shim,
        }
    }

    pub fn name(&self) -> &'static str {
        match self {
            Self::Direct { .. } => "direct",
            Self::SystemdScope { .. } => "systemd",
        }
    }

    fn shim_command(&self, unit: &str) -> Command {
        match self {
            Self::Direct { shim } => crate::child::command(shim),
            Self::SystemdScope { shim } => {
                let mut command = crate::child::command("systemd-run");
                // The unit's name is new every time: a second shim for a turn must be able to
                // start, find the turn taken and leave, which is how a turn starts only once.
                command
                    .args([
                        "--user",
                        "--scope",
                        "--quiet",
                        "--collect",
                        // The tool's arguments are passed on as they are. Without this,
                        // systemd-run reads `${...}` in them as its own variables and empties
                        // them: a prompt or a script would arrive changed.
                        "--expand-environment=no",
                        "--unit",
                        unit,
                        "--",
                    ])
                    .arg(shim);
                command
            }
        }
    }

    /// The same launcher and lifetime scope used by an ordinary turn. No
    /// per-turn identity is carried in the environment of this persistent process.
    pub(crate) fn goal_lane_command(
        &self,
        socket: &Path,
        program: &str,
        options: &Path,
        cwd: &Path,
    ) -> Command {
        let unit = format!("clarp-codex-goal-{}", ids::hex(4));
        self.lane_command(&unit, "codex-goal-lane", socket, program, options, cwd)
    }

    /// The command that starts a lane process of the shim's `subcommand` (`codex-goal-lane`,
    /// `opencode-lane`), in a scope of its own named `unit` that outlives the turn that started
    /// it.
    pub(crate) fn lane_command(
        &self,
        unit: &str,
        subcommand: &str,
        socket: &Path,
        program: &str,
        options: &Path,
        cwd: &Path,
    ) -> Command {
        let mut command = self.shim_command(unit);
        command
            .arg(subcommand)
            .arg(socket)
            .arg(program)
            .arg(options)
            .current_dir(cwd)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        command
    }

    /// A Claude lane, under the same launcher and lifetime scope as an ordinary turn: the
    /// shim keeps one Claude Code for the agent between its turns. As for a Codex lane, no
    /// turn's identity is carried in its environment.
    pub(crate) fn claude_lane_command(&self, socket: &Path, options: &Path, cwd: &Path) -> Command {
        let mut command = self.shim_command(&format!("clarp-claude-lane-{}", ids::hex(4)));
        command
            .arg("claude-lane")
            .arg(socket)
            .arg(options)
            .current_dir(cwd)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        command
    }

    /// The command that starts a shim for this order.
    pub fn command(&self, order: &ShimOrder) -> Command {
        let mut command =
            self.shim_command(&format!("clarp-turn-{}-{}", order.turn.0, ids::hex(4)));
        if order.prompt_as_file {
            command.arg("--prompt-as-file");
        }
        if order.streams_input {
            command.args(["--stdin-until", "result"]);
        }
        if let Some(session) = &order.named_session {
            command.arg("--named-session").arg(session);
        }
        if let Some((signal, wait_ms)) = order.before_stop {
            command.args(["--before-stop", signal, &wait_ms.to_string()]);
        }
        let program = if order.program == "clarp-shim" {
            match self {
                Self::Direct { shim } | Self::SystemdScope { shim } => shim.as_path(),
            }
        } else {
            Path::new(&order.program)
        };
        command
            .arg("--spool")
            .arg(&order.spool)
            .arg("--socket")
            .arg(&order.socket)
            .arg("--cwd")
            .arg(&order.cwd)
            .arg("--stdin-file")
            .arg(&order.stdin_file)
            .arg("--")
            .arg(program)
            .args(&order.args);

        for (name, value) in &order.env {
            command.env(name, value);
        }
        command
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        command
    }

    /// Starts a shim and waits until it says the turn is its own and the tool is up, or ends.
    /// A Stop of the turn that came before the shim was started starts nothing; one that came
    /// while it started is given to it as soon as it is ready, before anyone follows it.
    pub fn launch(&self, order: &ShimOrder, starting: &Starting) -> Launched {
        if let Some(dir) = order.socket.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        if !starting.may_start() {
            return Launched::Stopped;
        }
        let mut child = match self.command(order).spawn() {
            Ok(child) => child,
            Err(error) => {
                return Launched::NoShim(format!("the shim could not be started: {error}"));
            }
        };
        let mut said = String::new();
        if let Some(out) = child.stdout.take() {
            let _ = BufReader::new(out).read_line(&mut said);
        }
        if said.trim() == "ready" {
            if let Some(grace_ms) = starting.started() {
                ask_to_stop(&order.socket, grace_ms);
            }
            return Launched::Ready(child);
        }
        let launched = match child.wait() {
            Ok(status) => match status.code() {
                Some(code) if code == i32::from(EXIT_TURN_TAKEN) => Launched::TurnTaken,
                Some(code) if code == i32::from(EXIT_NOT_STARTED) => Launched::SaidWhy,
                code => Launched::Failed(format!(
                    "the shim ended before it had the turn (exit {code:?})"
                )),
            },
            Err(error) => Launched::Failed(format!("the shim was lost: {error}")),
        };
        if matches!(launched, Launched::TurnTaken) {
            // The shim that has the turn makes its spool before it listens (issue 726). Until
            // it listens this launch is still starting, so a Stop that comes meanwhile is kept;
            // once it listens, that Stop is told to it, as to a shim of this launch's own, and
            // it is followed from there.
            let asked = if listens_within(&order.socket, TAKEN_LISTENS_WITHIN) {
                starting.started()
            } else {
                starting.over()
            };
            if let Some(grace_ms) = asked {
                ask_to_stop(&order.socket, grace_ms);
            }
            return launched;
        }
        // A Stop asked of a launch that came to nothing goes with it: no later launch is
        // stopped by it.
        let _ = starting.over();
        launched
    }

    /// Starts a shim for a turn and follows it to its end. Blocks for as long as the turn runs.
    pub fn launch_and_follow(&self, order: &ShimOrder, starting: &Starting, tell: &dyn Fn(Told)) {
        match self.launch(order, starting) {
            Launched::Ready(mut child) => {
                follow(&order.socket, tell);
                let _ = child.wait();
            }
            Launched::TurnTaken => follow(&order.socket, tell),
            Launched::SaidWhy => tell(Told::SpoolChanged),
            Launched::Failed(reason) | Launched::NoShim(reason) => tell(Told::Failed(reason)),
            // Nothing was started: the turn is gone at once, as one held for an account is.
            Launched::Stopped => tell(Told::Gone),
        }
    }
}

pub(crate) async fn gate_steering(order: &mut ShimOrder, versions: &crate::cli_version::Cache) {
    if !order.args.iter().any(|arg| arg == REPLAY_USER_MESSAGES) {
        return;
    }
    // This is the launch thread. Dispatch consults what the shim recorded; it does not probe.
    let version = versions.answer(PathBuf::from(&order.program)).await;
    if !version.is_some_and(|version| clarp_kernel::steering::supported_version(&version)) {
        order.args.retain(|arg| arg != REPLAY_USER_MESSAGES);
        // A Claude Code that would not say what it read is given its one message and no more.
        order.streams_input = false;
    }
}

/// How long a launch that found its turn taken waits for the shim that took it to listen.
const TAKEN_LISTENS_WITHIN: Duration = Duration::from_secs(10);

/// Follows the shim at this socket until it is gone: says when its spool may have grown, and
/// at the end that nothing answers any more. Blocks for as long as the shim lives.
pub fn follow(socket: &Path, tell: &dyn Fn(Told)) {
    if let Ok(mut shim) = UnixStream::connect(socket) {
        let asked = serde_json::to_string(&Request::Follow)
            .map(|line| shim.write_all(format!("{line}\n").as_bytes()).is_ok())
            .unwrap_or(false);
        if asked {
            // Everything the shim sends on this connection is a hint, its reply included.
            let mut hints = [0u8; 4096];
            while matches!(shim.read(&mut hints), Ok(read) if read > 0) {
                tell(Told::SpoolChanged);
                std::thread::sleep(STRIDE);
            }
        }
    }
    // Whatever the shim wrote last, and then the news that it is gone.
    tell(Told::SpoolChanged);
    tell(Told::Gone);
}

/// A bounded takeover exchange on a fresh connection. An absent reply is uncertain
/// because the shim may already have recorded the decision.
pub(crate) fn takeover_request(socket: &Path, request: Request) -> Option<Reply> {
    let mut shim = UnixStream::connect(socket).ok()?;
    shim.set_read_timeout(Some(STOP_TIMEOUT)).ok()?;
    shim.set_write_timeout(Some(STOP_TIMEOUT)).ok()?;
    let mut line = serde_json::to_vec(&request).ok()?;
    line.push(b'\n');
    shim.write_all(&line).ok()?;
    let mut answer = String::new();
    BufReader::new(shim)
        .take(4096)
        .read_line(&mut answer)
        .ok()?;
    serde_json::from_str(&answer).ok()
}

/// Asks the shim at this socket to end its tool. A shim that is not there has nothing to end.
/// A turn whose shim may still be starting is stopped through its launch instead
/// ([`Launches::stop`]).
pub fn stop(socket: &Path, grace_ms: u64) {
    ask_to_stop(socket, grace_ms);
}

/// Sends a Stop to the shim at this socket, if one is there.
fn ask_to_stop(socket: &Path, grace_ms: u64) {
    let Ok(mut shim) = UnixStream::connect(socket) else {
        return;
    };
    let _ = shim.set_read_timeout(Some(STOP_TIMEOUT));
    let _ = shim.set_write_timeout(Some(STOP_TIMEOUT));
    let Ok(line) = serde_json::to_string(&Request::Stop { grace_ms }) else {
        return;
    };
    if shim.write_all(format!("{line}\n").as_bytes()).is_ok() {
        let mut reply = String::new();
        let _ = BufReader::new(shim).read_line(&mut reply);
    }
}

/// How lately a turn's spool was written for the shim that wrote it to be taken as one that may
/// still be starting (issue 728): a shim makes its spool before it listens.
pub const STARTING_SPOOL: Duration = Duration::from_secs(30);

/// Whether a shim of this turn may still be starting: one of these spools was written within
/// [`STARTING_SPOOL`]. Only the files' times are read: a spool is never opened, and its lock
/// is never taken or tested, since a shim that starts could lose its turn to that.
pub fn may_be_starting(spools: &[PathBuf]) -> bool {
    let now = std::time::SystemTime::now();
    spools.iter().any(|spool| {
        std::fs::metadata(spool)
            .and_then(|meta| meta.modified())
            .is_ok_and(|at| now.duration_since(at).unwrap_or_default() < STARTING_SPOOL)
    })
}

/// The spools of every attempt of a turn in this agent's spool directory (`7.jsonl`,
/// `7.2.jsonl`, the fork's `7.1001.jsonl`), as the directory lists them.
pub fn spools_of(dir: &Path, turn: TurnId) -> Vec<PathBuf> {
    let (whole, attempts) = (format!("{}.jsonl", turn.0), format!("{}.", turn.0));
    let Ok(listed) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    listed
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| {
                    name == whole
                        || name.strip_prefix(&attempts).is_some_and(|rest| {
                            rest.strip_suffix(".jsonl").is_some_and(|n| {
                                !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit())
                            })
                        })
                })
        })
        .collect()
}

/// Follows the shim of a turn that this Host did not launch: one that ran on while the Host was
/// away (issue 728). A shim that may still be starting is given until it listens, as a launch
/// that found its turn taken gives it (issue 726); one long gone is found gone at once.
pub fn follow_rejoined(socket: &Path, starting: bool, tell: &dyn Fn(Told)) {
    if starting {
        let _ = listens_within(socket, TAKEN_LISTENS_WITHIN);
    }
    follow(socket, tell);
}

/// Stops the shim of a turn that this Host did not launch: a shim that may still be starting is
/// told once it listens, so that the Stop is not lost in the moment before it does.
pub fn stop_rejoined(socket: &Path, grace_ms: u64, starting: bool) {
    if starting {
        let _ = listens_within(socket, TAKEN_LISTENS_WITHIN);
    }
    ask_to_stop(socket, grace_ms);
}

/// Whether a shim answers at this socket, asked again for up to `within` while none does.
fn listens_within(socket: &Path, within: Duration) -> bool {
    let until = std::time::Instant::now() + within;
    loop {
        if takeover_request(socket, Request::Ping).is_some() {
            return true;
        }
        if std::time::Instant::now() >= until {
            return false;
        }
        std::thread::sleep(STRIDE);
    }
}

/// Where one launch of a turn's shim stands, as far as a Stop of that turn is concerned. The
/// agent hands it to the launch and keeps it in its [`Launches`] (issue 722: a Stop that came
/// before the shim listened on its socket found nothing there and was lost, and the turn it
/// had said it ended ran on).
#[derive(Debug, Clone, Default)]
pub struct Starting(Arc<Mutex<Start>>);

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
enum Start {
    /// The shim is not started yet, or not ready.
    #[default]
    Starting,
    /// A Stop came meanwhile, with this grace.
    StopAsked(u64),
    /// The shim said it is ready: a Stop goes to its socket.
    Started,
    /// The launch came to nothing.
    Over,
}

impl Starting {
    fn state(&self) -> MutexGuard<'_, Start> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// A Stop of this launch's turn. Answers `true` when the launch carries it out: it starts
    /// nothing, or tells the shim as soon as it is ready, once however many Stops come.
    /// `false`: the launch is over, and the Stop is for the shim's socket, if any.
    pub fn stop(&self, grace_ms: u64) -> bool {
        let mut state = self.state();
        match *state {
            Start::Starting => {
                *state = Start::StopAsked(grace_ms);
                true
            }
            Start::StopAsked(_) => true,
            Start::Started | Start::Over => false,
        }
    }

    /// Whether the shim may still be started. One stopped first never is.
    fn may_start(&self) -> bool {
        let mut state = self.state();
        if matches!(*state, Start::StopAsked(_)) {
            *state = Start::Over;
        }
        *state == Start::Starting
    }

    /// The shim is ready. Answers the grace of a Stop that came while it started.
    fn started(&self) -> Option<u64> {
        let mut state = self.state();
        let asked = match *state {
            Start::StopAsked(grace_ms) => Some(grace_ms),
            _ => None,
        };
        *state = Start::Started;
        asked
    }

    /// The launch came to nothing. Answers the grace of a Stop that came meanwhile, which
    /// goes with it.
    fn over(&self) -> Option<u64> {
        let mut state = self.state();
        let asked = match *state {
            Start::StopAsked(grace_ms) => Some(grace_ms),
            _ => None,
        };
        *state = Start::Over;
        asked
    }

    /// Whether a launch still holds this and has not come to the end of starting.
    fn under_way(&self) -> bool {
        Arc::strong_count(&self.0) > 1
            && matches!(*self.state(), Start::Starting | Start::StopAsked(_))
    }
}

/// An agent's launches whose shims are starting, each by its turn and socket.
#[derive(Debug, Default)]
pub struct Launches(Vec<(TurnId, PathBuf, Starting)>);

impl Launches {
    /// A launch of this turn's shim at this socket begins: what it is to be handed.
    pub fn begin(&mut self, turn: TurnId, socket: &Path) -> Starting {
        self.0.retain(|(_, _, starting)| starting.under_way());
        let starting = Starting::default();
        self.0.push((turn, socket.to_path_buf(), starting.clone()));
        starting
    }

    /// A Stop of this turn, whose shim listens at this socket. Answers `true` when a launch of
    /// that very turn is starting the shim and carries the Stop out; `false` when the Stop is
    /// for the socket.
    pub fn stop(&mut self, turn: TurnId, socket: &Path, grace_ms: u64) -> bool {
        self.0.retain(|(_, _, starting)| starting.under_way());
        self.0
            .iter()
            .find(|(own, at, _)| *own == turn && at == socket)
            .is_some_and(|(_, _, starting)| starting.stop(grace_ms))
    }
}

/// Try once. After an ambiguous socket outcome the actor still waits for stdout evidence.
pub async fn steer(socket: &Path, input: clarp_actor::Steering) {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt};

    let request = match input {
        clarp_actor::Steering::Stdin(data) => Request::Stdin { data },
        clarp_actor::Steering::Words { id, text } => Request::Steer { id, text },
    };
    let Ok(line) = serde_json::to_string(&request) else {
        return;
    };
    let attempt = async {
        let mut shim = tokio::net::UnixStream::connect(socket).await?;
        shim.write_all(format!("{line}\n").as_bytes()).await?;
        let mut reply = String::new();
        tokio::io::BufReader::new(shim).read_line(&mut reply).await
    };
    let _ = tokio::time::timeout(STOP_TIMEOUT, attempt).await;
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use clarp_kernel::turn::TurnId;

    use super::*;

    fn order() -> ShimOrder {
        ShimOrder {
            opening: None,
            named_session: None,
            model: None,
            effort: None,
            prompt_as_file: false,
            streams_input: false,
            before_stop: None,
            turn: TurnId(7),
            attempt: 1,
            spool: PathBuf::from("/data/agents/a1/spool/7.jsonl"),
            socket: PathBuf::from("/run/a1-7.sock"),
            stdin_file: PathBuf::from("/data/agents/a1/spool/7.stdin"),
            cwd: PathBuf::from("/work"),
            program: "claude".to_string(),
            args: vec!["-p".to_string()],
            env: vec![("CLAUDE_PWA_SESSION".to_string(), "maximus".to_string())],
        }
    }

    fn words(command: &Command) -> Vec<String> {
        std::iter::once(command.get_program())
            .chain(command.get_args())
            .map(|word| word.to_string_lossy().into_owned())
            .collect()
    }

    #[test]
    fn a_turn_that_takes_more_messages_keeps_its_input_open_until_its_first_result() {
        let direct = Launcher::by_name("direct", PathBuf::from("/bin/clarp-shim")).unwrap();
        let mut order = order();
        order.program = "clarp-shim".into();
        order.args = vec!["agy-acp-turn".into(), "agy".into()];
        order.streams_input = true;
        let given = words(&direct.command(&order));
        let at = given
            .iter()
            .position(|word| word == "--stdin-until")
            .unwrap();
        assert_eq!(given[at + 1], "result");
        assert!(at < given.iter().position(|word| word == "--").unwrap());
        order.streams_input = false;
        let given = words(&direct.command(&order));
        assert!(!given.iter().any(|word| word == "--stdin-until"));
    }

    #[test]
    fn a_shim_is_started_with_everything_its_turn_needs() {
        let direct = Launcher::by_name("direct", PathBuf::from("/bin/clarp-shim")).unwrap();
        assert_eq!(
            words(&direct.command(&order())),
            vec![
                "/bin/clarp-shim",
                "--spool",
                "/data/agents/a1/spool/7.jsonl",
                "--socket",
                "/run/a1-7.sock",
                "--cwd",
                "/work",
                "--stdin-file",
                "/data/agents/a1/spool/7.stdin",
                "--",
                "claude",
                "-p"
            ]
        );
        let command = direct.command(&order());
        let given: Vec<_> = command
            .get_envs()
            .filter(|(_, value)| value.is_some())
            .collect();
        let configured = crate::child_environment()
            .iter()
            .filter(|(_, value)| value.is_some())
            .count();
        assert_eq!(
            given.len(),
            configured + 1,
            "the agent and its Host settings"
        );
    }

    #[test]
    fn a_shim_is_never_given_the_hosts_own_secrets() {
        assert!(crate::OWN_SECRETS.contains(&"CLARP_HOST_TOKEN"));
        assert!(crate::OWN_SECRETS.contains(&"CLARP_PUSH_CREDENTIAL"));
        for launcher in ["direct", "systemd"] {
            let command = Launcher::by_name(launcher, PathBuf::from("/bin/clarp-shim"))
                .unwrap()
                .command(&order());
            // Taken out of what the shim inherits, whatever the Host's environment holds.
            let removed: Vec<String> = command
                .get_envs()
                .filter(|(_, value)| value.is_none())
                .map(|(key, _)| key.to_string_lossy().into_owned())
                .collect();
            for secret in crate::OWN_SECRETS {
                assert!(
                    removed.iter().any(|key| key == secret),
                    "{launcher}: {secret}"
                );
            }
        }
    }

    #[test]
    fn a_shim_uses_its_own_turn_identity_and_inherits_provider_settings() {
        // Supply a foreign parent to this test in a separate process so other
        // tests can launch concurrently with their own environment.
        const HERE: &str =
            "launcher::tests::a_shim_uses_its_own_turn_identity_and_inherits_provider_settings";
        if std::env::var_os("FIXTURE_LAUNCH_PARENT").is_none() {
            let status = Command::new(std::env::current_exe().unwrap())
                .env_clear()
                .args(["--exact", HERE])
                .env("FIXTURE_LAUNCH_PARENT", "1")
                .env("CLARP_BACKGROUND_ORIGIN", "foreign-origin")
                .env("CLARP_SESSION", "foreign-session")
                .env("CLARP_PROVIDER_TURN", "foreign-turn")
                .env("CLAUDE_PWA_SESSION", "foreign-session")
                .env("CLARP_AGY_MANAGED_TURN", "foreign-agy")
                .env("CLARP_BACKGROUND_WORKER_PID", "1111")
                .env("CLAUDE_CODE_SESSION_ID", "foreign-claude")
                .env("CLARP_CODE_ROOT", "/fixture/foreign-code")
                .env("CLAUDE_PWA_DB", "/fixture/foreign-db")
                .env("CLAUDE_PWA_LOG_DIR", "/fixture/foreign-logs")
                .env("CLARP_CACHE_DIR", "/fixture/foreign-cache")
                .env("CLARP_DATA_DIR", "/fixture/foreign-data")
                .env("CLAUDE_PWA_CONFIG", "/fixture/host.toml")
                .env("CLARP_CONFIG_DIR", "/fixture/config")
                .env("CLARP_SHARE_DIR", "/fixture/share")
                .env("CLARP_QA_PROVIDER_ROOT", "/fixture/provider")
                .env("CLAUDE_PWA_EXTRA", "fixture-setting")
                .env(clarp_backend::MANAGED_CLAUDE_ENV, "foreign-marker")
                .status()
                .unwrap();
            assert!(status.success());
            return;
        }
        for mode in ["direct", "systemd"] {
            let launcher = Launcher::by_name(mode, "/bin/clarp-shim".into()).unwrap();
            let mut turn = order();
            turn.env = vec![
                ("CLAUDE_PWA_SESSION".into(), "fixture".into()),
                ("CLARP_SESSION".into(), "fixture".into()),
                ("CLARP_PROVIDER_TURN".into(), "own-token".into()),
            ];
            for claude in [false, true] {
                if claude {
                    turn.env
                        .push((clarp_backend::MANAGED_CLAUDE_ENV.into(), "1".into()));
                }
                let command = launcher.command(&turn);
                let env: std::collections::BTreeMap<_, _> = command.get_envs().collect();
                let value = |key: &str| env.get(std::ffi::OsStr::new(key)).copied();
                for removed in [
                    "CLARP_BACKGROUND_ORIGIN",
                    "CLARP_AGY_MANAGED_TURN",
                    "CLARP_BACKGROUND_WORKER_PID",
                    "CLAUDE_CODE_SESSION_ID",
                    "CLARP_CODE_ROOT",
                    "CLAUDE_PWA_DB",
                    "CLAUDE_PWA_LOG_DIR",
                    "CLARP_CACHE_DIR",
                    "CLARP_DATA_DIR",
                ] {
                    assert_eq!(value(removed), Some(None));
                }
                // Each configured store value is explicitly in the launch order.
                for (key, path) in [
                    ("CLAUDE_PWA_CONFIG", "/fixture/host.toml"),
                    ("CLARP_CONFIG_DIR", "/fixture/config"),
                    ("CLARP_SHARE_DIR", "/fixture/share"),
                ] {
                    assert_eq!(value(key), Some(Some(std::ffi::OsStr::new(path))));
                }
                for inherited in ["CLARP_QA_PROVIDER_ROOT", "CLAUDE_PWA_EXTRA"] {
                    assert_eq!(value(inherited), None);
                }
                assert_eq!(
                    value(clarp_backend::MANAGED_CLAUDE_ENV),
                    Some(claude.then_some(std::ffi::OsStr::new("1")))
                );
                for (key, want) in [
                    ("CLARP_SESSION", "fixture"),
                    ("CLAUDE_PWA_SESSION", "fixture"),
                    ("CLARP_PROVIDER_TURN", "own-token"),
                ] {
                    assert_eq!(value(key), Some(Some(std::ffi::OsStr::new(want))));
                }
            }
        }
    }

    #[test]
    fn under_systemd_a_shim_gets_a_scope_of_its_own_with_a_name_of_its_own() {
        let systemd = Launcher::by_name("systemd", PathBuf::from("/bin/clarp-shim")).unwrap();
        let first = words(&systemd.command(&order()));
        let second = words(&systemd.command(&order()));
        assert_eq!(
            &first[..7],
            [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                "--expand-environment=no",
                "--unit"
            ],
            "and what the tool is given is not systemd's to rewrite"
        );
        assert!(first[7].starts_with("clarp-turn-7-"));
        assert_ne!(
            first[7], second[7],
            "two launches for one turn do not collide"
        );
        assert_eq!(&first[8..10], ["--", "/bin/clarp-shim"]);
        assert!(Launcher::by_name("docker", PathBuf::new()).is_none());
    }

    #[test]
    fn a_shim_that_is_not_there_is_gone_and_a_stop_to_it_is_nothing() {
        let told = std::sync::Mutex::new(Vec::new());
        follow(Path::new("/nonexistent/clarp.sock"), &|what| {
            told.lock().unwrap().push(what);
        });
        assert_eq!(*told.lock().unwrap(), vec![Told::SpoolChanged, Told::Gone]);
        stop(Path::new("/nonexistent/clarp.sock"), 1_000);

        let missing =
            Launcher::by_name("direct", PathBuf::from("/nonexistent/clarp-shim")).unwrap();
        let mut order = order();
        order.socket = std::env::temp_dir().join("clarp-host-launcher-test/none.sock");
        assert!(matches!(
            missing.launch(&order, &Starting::default()),
            Launched::NoShim(_)
        ));
    }

    /// A stand-in for the shim, for the tests of a Stop that comes while a shim starts. It
    /// notes in its place that it was started, takes `delay` seconds before it listens, and
    /// either leaves with `exit` or says it is ready and notes every request it is given for
    /// a second and a half after the last one.
    struct StandIn {
        dir: PathBuf,
        launcher: Launcher,
        order: ShimOrder,
    }

    impl StandIn {
        fn new(name: &str, delay: f32, exit: Option<u8>) -> Self {
            let dir =
                std::env::temp_dir().join(format!("clarp-launcher-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).unwrap();
            let python = std::env::split_paths(&std::env::var_os("PATH").unwrap())
                .map(|path| path.join("python3"))
                .find(|path| path.is_file())
                .expect("python3 for the stand-in shim");
            let leave = exit.map_or(String::new(), |code| format!("sys.exit({code})"));
            let script = r#"#!@python@
import os, socket, sys, time
at = sys.argv[sys.argv.index("--socket") + 1]
here = os.path.dirname(at)
open(os.path.join(here, "started"), "a").write("started\n")
time.sleep(@delay@)
@leave@
server = socket.socket(socket.AF_UNIX)
server.bind(at)
server.listen(8)
print("ready", flush=True)
server.settimeout(1.5)
try:
    while True:
        peer, _ = server.accept()
        line = peer.makefile().readline()
        open(os.path.join(here, "asked"), "a").write(line)
        peer.sendall(b'{"ok": true}\n')
        peer.close()
except socket.timeout:
    pass
"#
            .replace("@python@", &python.display().to_string())
            .replace("@delay@", &delay.to_string())
            .replace("@leave@", &leave);
            let shim = dir.join("stand-in");
            std::fs::write(&shim, script).unwrap();
            std::fs::set_permissions(&shim, std::os::unix::fs::PermissionsExt::from_mode(0o755))
                .unwrap();
            let mut order = order();
            order.socket = dir.join("turn.sock");
            order.spool = dir.join("turn.jsonl");
            order.stdin_file = dir.join("turn.stdin");
            order.cwd = dir.clone();
            Self {
                launcher: Launcher::by_name("direct", shim).unwrap(),
                dir,
                order,
            }
        }

        fn read(&self, name: &str) -> Vec<String> {
            std::fs::read_to_string(self.dir.join(name))
                .unwrap_or_default()
                .lines()
                .map(str::to_string)
                .collect()
        }

        /// Launches in the background, and waits until the stand-in has been started.
        fn launch_in_background(&self, starting: &Starting) -> std::thread::JoinHandle<Launched> {
            let (launcher, order, starting) =
                (self.launcher.clone(), self.order.clone(), starting.clone());
            let launch = std::thread::spawn(move || launcher.launch(&order, &starting));
            let deadline = std::time::Instant::now() + Duration::from_secs(20);
            while self.read("started").is_empty() {
                assert!(
                    std::time::Instant::now() < deadline,
                    "the stand-in never started"
                );
                std::thread::sleep(Duration::from_millis(10));
            }
            launch
        }
    }

    impl Drop for StandIn {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    #[test]
    fn a_turn_stopped_before_its_shim_is_started_starts_nothing() {
        let stand_in = StandIn::new("first", 0.0, None);
        let starting = Starting::default();
        assert!(starting.stop(1_000), "the launch carries the Stop out");
        assert!(matches!(
            stand_in.launcher.launch(&stand_in.order, &starting),
            Launched::Stopped
        ));
        assert!(stand_in.read("started").is_empty(), "nothing was started");
        let told = Mutex::new(Vec::new());
        stand_in
            .launcher
            .launch_and_follow(&stand_in.order, &starting, &|what| {
                told.lock().unwrap().push(what);
            });
        assert_eq!(
            *told.lock().unwrap(),
            vec![Told::Gone],
            "the turn is gone at once"
        );
        assert!(stand_in.read("started").is_empty());
        assert!(
            !starting.stop(1_000),
            "over: a later Stop is for the socket"
        );
    }

    #[test]
    fn stops_that_come_while_the_shim_starts_reach_it_once_when_it_is_ready() {
        let stand_in = StandIn::new("starting", 2.0, None);
        let starting = Starting::default();
        let launch = stand_in.launch_in_background(&starting);
        assert!(starting.stop(4_321));
        assert!(starting.stop(1_234), "a second Stop is the first one's");
        let Launched::Ready(mut child) = launch.join().unwrap() else {
            panic!("the stand-in was to say it is ready");
        };
        assert!(child.wait().unwrap().success());
        let asked = stand_in.read("asked");
        assert_eq!(asked.len(), 1, "told once: {asked:?}");
        let asked: serde_json::Value = serde_json::from_str(&asked[0]).unwrap();
        assert_eq!(asked["grace_ms"], 4_321, "{asked}");
        assert!(
            !starting.stop(1_000),
            "started: a later Stop is for the socket"
        );
    }

    #[test]
    fn a_launch_that_comes_to_nothing_drops_its_stop_and_no_later_launch_is_stopped_by_it() {
        let failing = StandIn::new("failing", 2.0, Some(3));
        let starting = Starting::default();
        let launch = failing.launch_in_background(&starting);
        assert!(starting.stop(1_000));
        assert!(matches!(launch.join().unwrap(), Launched::Failed(_)));
        assert!(!starting.stop(1_000), "the launch is over");
        drop(starting);
        // The next launch at the same socket begins anew, and nobody stops it.
        let again = StandIn::new("after-failing", 0.0, None);
        let mut launches = Launches::default();
        let next = launches.begin(TurnId(7), &again.order.socket);
        let Launched::Ready(mut child) = again.launcher.launch(&again.order, &next) else {
            panic!("the stand-in was to say it is ready");
        };
        assert!(child.wait().unwrap().success());
        assert!(again.read("asked").is_empty(), "nothing asked it to stop");
    }

    #[test]
    fn a_launch_that_finds_its_turn_taken_follows_the_shim_that_took_it_once_it_listens() {
        // The shim that took the turn made its spool, and listens two seconds later (726).
        let first = StandIn::new("taken-first", 2.0, None);
        let launch = first.launch_in_background(&Starting::default());
        let mut second = StandIn::new("taken-second", 0.0, Some(EXIT_TURN_TAKEN));
        second.order.socket = first.order.socket.clone();
        let told = Mutex::new(Vec::new());
        second
            .launcher
            .launch_and_follow(&second.order, &Starting::default(), &|what| {
                told.lock().unwrap().push(what);
            });
        let asked = first.read("asked");
        assert!(
            asked.iter().any(|line| line.contains(r#""op":"follow""#)),
            "the first shim was followed: {asked:?}"
        );
        assert_eq!(told.lock().unwrap().last(), Some(&Told::Gone));
        let Launched::Ready(mut child) = launch.join().unwrap() else {
            panic!("the first stand-in was to say it is ready");
        };
        assert!(child.wait().unwrap().success());
    }

    #[test]
    fn a_stop_while_a_taken_turn_waits_for_its_shim_to_listen_reaches_that_shim() {
        // The second launch finds the turn taken by a shim that listens two seconds after it
        // started, and the turn is stopped in between (726).
        let first = StandIn::new("taken-stop-first", 2.0, None);
        let first_launch = first.launch_in_background(&Starting::default());
        let mut second = StandIn::new("taken-stop-second", 0.0, Some(EXIT_TURN_TAKEN));
        second.order.socket = first.order.socket.clone();
        let starting = Starting::default();
        let (launcher, order, held) = (
            second.launcher.clone(),
            second.order.clone(),
            starting.clone(),
        );
        let began = std::time::Instant::now();
        let launch = std::thread::spawn(move || launcher.launch(&order, &held));
        // Both stand-ins note their start in the first one's place; the second leaves at once.
        let deadline = std::time::Instant::now() + Duration::from_secs(20);
        while first.read("started").len() < 2 {
            assert!(
                std::time::Instant::now() < deadline,
                "the second never started"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        std::thread::sleep(Duration::from_millis(300));
        assert!(
            starting.stop(4_321),
            "still starting: the launch keeps the Stop"
        );
        assert!(matches!(launch.join().unwrap(), Launched::TurnTaken));
        assert!(
            began.elapsed() < Duration::from_secs(8),
            "the shim answered: the launch did not wait out its ten seconds"
        );
        let stops: Vec<String> = first
            .read("asked")
            .into_iter()
            .filter(|line| line.contains(r#""op":"stop""#))
            .collect();
        assert_eq!(stops.len(), 1, "told once: {stops:?}");
        let stop: serde_json::Value = serde_json::from_str(&stops[0]).unwrap();
        assert_eq!(stop["grace_ms"], 4_321, "{stop}");
        assert!(
            !starting.stop(1_000),
            "it listens: a later Stop is for the socket"
        );
        let Launched::Ready(mut child) = first_launch.join().unwrap() else {
            panic!("the first stand-in was to say it is ready");
        };
        assert!(child.wait().unwrap().success());
    }

    #[test]
    fn a_rejoined_shim_that_listens_late_is_followed_and_stopped_once_it_listens() {
        // A Host started again while a turn's shim was starting: the shim has made its spool
        // and listens two seconds later (728). A Stop and a follow without the wait find
        // nothing; with it, both reach the shim.
        let shim = StandIn::new("rejoin-late", 2.0, None);
        let launch = shim.launch_in_background(&Starting::default());
        let spool = shim.dir.join("7.jsonl");
        std::fs::write(&spool, "").unwrap();
        assert!(may_be_starting(std::slice::from_ref(&spool)));
        let socket = shim.order.socket.clone();
        stop_rejoined(&socket, 1, false);
        let (at, began) = (socket.clone(), std::time::Instant::now());
        let stopping = std::thread::spawn(move || stop_rejoined(&at, 4_321, true));
        let told = Mutex::new(Vec::new());
        follow_rejoined(&socket, true, &|what| told.lock().unwrap().push(what));
        stopping.join().unwrap();
        assert!(
            began.elapsed() < Duration::from_secs(8),
            "the shim answered: nothing waited out its ten seconds"
        );
        let asked = shim.read("asked");
        assert!(
            asked.iter().any(|line| line.contains(r#""op":"follow""#)),
            "followed: {asked:?}"
        );
        let stops: Vec<serde_json::Value> = asked
            .iter()
            .filter(|line| line.contains(r#""op":"stop""#))
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(
            stops.len(),
            1,
            "the Stop sent in the gap without waiting is lost: {asked:?}"
        );
        assert_eq!(stops[0]["grace_ms"], 4_321);
        assert_eq!(told.lock().unwrap().last(), Some(&Told::Gone));
        let Launched::Ready(mut child) = launch.join().unwrap() else {
            panic!("the stand-in was to say it is ready");
        };
        assert!(child.wait().unwrap().success());
    }

    #[test]
    fn a_rejoined_turn_whose_shim_is_long_gone_is_found_gone_at_once() {
        let dir = std::env::temp_dir().join(format!("clarp-launcher-gone-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let spool = dir.join("7.jsonl");
        std::fs::write(&spool, "").unwrap();
        let long_ago = std::time::SystemTime::now() - Duration::from_secs(600);
        std::fs::File::options()
            .write(true)
            .open(&spool)
            .unwrap()
            .set_modified(long_ago)
            .unwrap();
        assert!(!may_be_starting(std::slice::from_ref(&spool)));
        assert!(!may_be_starting(&[dir.join("8.jsonl")]), "no spool at all");
        let began = std::time::Instant::now();
        let told = Mutex::new(Vec::new());
        follow_rejoined(&dir.join("7.sock"), false, &|what| {
            told.lock().unwrap().push(what);
        });
        stop_rejoined(&dir.join("7.sock"), 1_000, false);
        assert!(began.elapsed() < Duration::from_secs(1));
        assert_eq!(*told.lock().unwrap(), vec![Told::SpoolChanged, Told::Gone]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_spools_of_a_turn_are_those_of_its_attempts_and_its_fork() {
        let dir =
            std::env::temp_dir().join(format!("clarp-launcher-spools-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        for name in [
            "7.jsonl",
            "7.2.jsonl",
            "7.1001.jsonl",
            "70.jsonl",
            "7.stdin",
            "7.x.jsonl",
            "8.jsonl",
        ] {
            std::fs::write(dir.join(name), "").unwrap();
        }
        let mut found: Vec<String> = spools_of(&dir, TurnId(7))
            .iter()
            .map(|path| path.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        found.sort();
        assert_eq!(found, ["7.1001.jsonl", "7.2.jsonl", "7.jsonl"]);
        assert!(spools_of(&dir.join("none"), TurnId(7)).is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_stop_is_carried_out_by_the_launch_of_its_own_turn_only() {
        let mut launches = Launches::default();
        let socket = Path::new("/run/a1-7.sock");
        let starting = launches.begin(TurnId(7), socket);
        assert!(
            !launches.stop(TurnId(8), socket, 1_000),
            "another turn's Stop"
        );
        assert!(
            !launches.stop(TurnId(7), Path::new("/run/a1-7-2.sock"), 1_000),
            "another attempt's socket"
        );
        assert!(
            starting.may_start(),
            "neither touched this launch: it may still start"
        );
        assert!(launches.stop(TurnId(7), socket, 1_000));
        assert!(!starting.may_start());
        // A launch whose thread is gone is no longer looked at.
        let gone = launches.begin(TurnId(9), socket);
        drop(gone);
        assert!(!launches.stop(TurnId(9), socket, 1_000));
    }
}
