//! Starting shims, following them, and telling them to stop.
//!
//! A shim is started by this process and is not this process's: it gets a session of its own,
//! and under systemd a scope of its own, so that stopping or restarting the Host leaves it and
//! its tool running. Everything here blocks, and runs on a thread that belongs to one turn.

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use clarp_actor::ShimOrder;
use clarp_shim::{EXIT_NOT_STARTED, EXIT_TURN_TAKEN, Request};

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

    pub fn name(&self) -> &'static str {
        match self {
            Self::Direct { .. } => "direct",
            Self::SystemdScope { .. } => "systemd",
        }
    }

    /// The command that starts a shim for this order.
    pub fn command(&self, order: &ShimOrder) -> Command {
        let mut command = match self {
            Self::Direct { shim } => Command::new(shim),
            Self::SystemdScope { shim } => {
                let mut command = Command::new("systemd-run");
                // The unit's name is new every time: a second shim for a turn must be able to
                // start, find the turn taken and leave, which is how a turn starts only once.
                let unit = format!("clarp-turn-{}-{}", order.turn.0, ids::hex(4));
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
                        &unit,
                        "--",
                    ])
                    .arg(shim);
                command
            }
        };
        if order.prompt_as_file {
            command.arg("--prompt-as-file");
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
        // The Host's own secrets stay with the Host. A turn is given what its tools need.
        for name in crate::OWN_SECRETS {
            command.env_remove(name);
        }
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
    pub fn launch(&self, order: &ShimOrder) -> Launched {
        if let Some(dir) = order.socket.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let mut child = match self.command(order).spawn() {
            Ok(child) => child,
            Err(error) => {
                return Launched::Failed(format!("the shim could not be started: {error}"));
            }
        };
        let mut said = String::new();
        if let Some(out) = child.stdout.take() {
            let _ = BufReader::new(out).read_line(&mut said);
        }
        if said.trim() == "ready" {
            return Launched::Ready(child);
        }
        match child.wait() {
            Ok(status) => match status.code() {
                Some(code) if code == i32::from(EXIT_TURN_TAKEN) => Launched::TurnTaken,
                Some(code) if code == i32::from(EXIT_NOT_STARTED) => Launched::SaidWhy,
                code => Launched::Failed(format!(
                    "the shim ended before it had the turn (exit {code:?})"
                )),
            },
            Err(error) => Launched::Failed(format!("the shim was lost: {error}")),
        }
    }

    /// Starts a shim for a turn and follows it to its end. Blocks for as long as the turn runs.
    pub fn launch_and_follow(&self, order: &ShimOrder, tell: &dyn Fn(Told)) {
        match self.launch(order) {
            Launched::Ready(mut child) => {
                follow(&order.socket, tell);
                let _ = child.wait();
            }
            Launched::TurnTaken => follow(&order.socket, tell),
            Launched::SaidWhy => tell(Told::SpoolChanged),
            Launched::Failed(reason) => tell(Told::Failed(reason)),
        }
    }
}

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

/// Asks the shim at this socket to end its tool. A shim that is not there has nothing to end.
pub fn stop(socket: &Path, grace_ms: u64) {
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

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use clarp_kernel::turn::TurnId;

    use super::*;

    fn order() -> ShimOrder {
        ShimOrder {
            opening: None,
            named_session: None,
            prompt_as_file: false,
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
        assert_eq!(given.len(), 1, "the tool is told which agent it works for");
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
        assert!(matches!(missing.launch(&order), Launched::Failed(_)));
    }
}
