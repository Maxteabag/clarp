//! The Claude Code driver of the lane layer (issue 761): one `claude -p --input-format
//! stream-json` per agent kept under the shim between its chat turns, while the owner's
//! `claude_chat_lane` is on. What every lane shares is in [`crate::lane`]; the lane process is
//! the shim's `claude-lane`.
//!
//! No turn waits for a cold lane: a turn with no lane there for its conversation starts its
//! Claude Code as the lane ([`Lane::start`]), and runs on it; starting the lane is the turn's
//! start, so nothing is started twice (issue 833). Where no lane may start (no place within the
//! limits, or a turn nobody waits on while the machine is under pressure), the turn runs in a
//! process of its own as before, and once that process is gone a lane may be started for the
//! next turn ([`Lane::warm`]).
//!
//! A turn that a kept process cannot run keeps a process of its own: an opening of a call, a
//! takeover or rewind (`--resume-session-at`), a turn that only reads (its edit guard is the
//! process's own settings), a compaction, and a turn of a Claude Code too old to say which
//! message a result answers.

use std::collections::HashMap;
use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::{Arc, LazyLock, Mutex, MutexGuard, PoisonError};

use clarp_actor::ShimOrder;
use clarp_shim::claude_lane::{Claim, Request, Snapshot, key as own_key};
use clarp_shim::lane::key;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use super::{Endpoint, Place, Settings, TURN_FILE_ENV, Tool};

/// What names the turn of a process of its own in its environment; a lane's tools read the
/// lane's turn file instead.
const TURN_ENV: &str = "CLARP_PROVIDER_TURN";

/// The flags that name a turn's conversation, and the takeover's that end a lane.
const SESSION_FLAGS: [&str; 2] = ["--resume", "--session-id"];
const TAKEOVER_FLAGS: [&str; 2] = ["--resume-session-at", "--resume-drops-turn"];

/// The login a lane runs under: it holds the pool's lock shared while it owns a turn, and
/// leaves when the account Claude Code's file names changed. Claude Code runs on the machine's
/// own login: no agent is bound to a provider of its own.
pub(crate) struct Login {
    pub lock: Option<PathBuf>,
    pub file: Option<PathBuf>,
    /// Claude Code's credentials file, the one the Host's own login watcher reads: a lane
    /// whose login there changed leaves too, whatever wrote the change.
    pub credentials: Option<PathBuf>,
}

/// What a turn's MCP servers are: whether the agent's choice is held to, and its choice.
pub(crate) type Mcp = (bool, Option<Vec<String>>);

/// One start of an agent's lane at a time, here: a start and what ends a lane for a terminal
/// or a move take it in turn, so a lane that starts never misses the end it was due.
static STARTS: LazyLock<Mutex<HashMap<PathBuf, Arc<Mutex<()>>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Lane {
    endpoint: Endpoint,
}

/// What a turn would start Claude Code with, as a lane is started with it: the conversation,
/// and the digest of everything else that would make one process differ from another.
#[derive(Clone, Debug)]
pub(crate) struct Fit {
    pub session: String,
    pub digest: String,
}

/// The conversation an order runs in, and what it starts Claude Code with, where a kept
/// process can run it.
pub(crate) fn fits(order: &ShimOrder, mcp: &Mcp) -> Option<Fit> {
    if order.opening.is_some()
        || !order.streams_input
        || !order
            .args
            .iter()
            .any(|arg| arg == clarp_kernel::steering::REPLAY_USER_MESSAGES)
        || order
            .args
            .iter()
            .any(|arg| TAKEOVER_FLAGS.contains(&arg.as_str()))
        || order
            .env
            .iter()
            .any(|(name, _)| name == clarp_actor::fork::FORK_ENV)
    {
        return None;
    }
    let session = flag(&order.args, "--resume").or_else(|| flag(&order.args, "--session-id"))?;
    Some(Fit {
        session: session.to_owned(),
        digest: digest(
            order,
            mcp,
            &crate::mcp::definitions(mcp.0, mcp.1.as_deref()),
        ),
    })
}

fn flag<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
    args.iter()
        .position(|arg| arg == name)
        .and_then(|at| args.get(at + 1))
        .map(String::as_str)
        .filter(|value| !value.is_empty())
}

/// The arguments without the conversation's own flags: what two turns of one conversation
/// share when nothing about the agent changed.
fn without_session(args: &[String]) -> Vec<&str> {
    let mut kept = Vec::with_capacity(args.len());
    let mut skip = false;
    for arg in args {
        if std::mem::take(&mut skip) {
            continue;
        }
        if SESSION_FLAGS.contains(&arg.as_str()) {
            skip = true;
            continue;
        }
        kept.push(arg.as_str());
    }
    kept
}

/// The environment a lane's Claude Code runs with: the turn's, without the turn's identity.
fn lane_env(order: &ShimOrder) -> Vec<(String, String)> {
    let mut env: Vec<_> = order
        .env
        .iter()
        .filter(|(name, _)| name != TURN_ENV)
        .cloned()
        .collect();
    env.sort();
    env
}

/// The digest of what a lane is started with: the program, its arguments but the
/// conversation's (model, effort, identity, hooks among them), where it runs, its environment
/// (the Host's address and token among them) and the agent's MCP servers, both its choice and
/// what the catalog defines them as now (`servers`, [`crate::mcp::definitions`]). A turn whose
/// digest differs is not run on the lane, and the lane is started again after it.
pub(crate) fn digest(order: &ShimOrder, mcp: &Mcp, servers: &Value) -> String {
    let said = json!({
        "program": order.program,
        "args": without_session(&order.args),
        "cwd": order.cwd,
        "env": lane_env(order),
        "mcp": [mcp.0, mcp.1],
        "servers": servers,
    });
    format!("{:x}", Sha256::digest(said.to_string().as_bytes()))
}

/// The file Claude Code says who is logged in in: a lane whose account changed leaves.
pub(crate) fn login_file(home: &Path) -> PathBuf {
    if let Some(named) = std::env::var_os("CLAUDE_CONFIG_FILE").filter(|named| !named.is_empty()) {
        return PathBuf::from(named);
    }
    if let Some(dir) = std::env::var_os("CLAUDE_CONFIG_DIR").filter(|dir| !dir.is_empty()) {
        let inside = PathBuf::from(dir).join(".claude.json");
        if inside.is_file() {
            return inside;
        }
    }
    home.join(".claude.json")
}

fn threads(run: &Path) -> PathBuf {
    run.join("claude-threads")
}

/// A turn of a process of its own takes its conversation's lock shared, and is refused while
/// a lane holds it: one writer per conversation.
/// The layer's lock ([`super::guard_exec`]); a Claude turn's program is never the shim itself.
pub(crate) fn guard_turn(order: &mut ShimOrder, run: &Path, session: &str) -> io::Result<()> {
    super::guard_exec(order, &threads(run), session, Path::new("clarp-shim"))
}

impl Lane {
    pub(crate) fn of(run: &Path, agent_id: &str) -> Self {
        Self {
            endpoint: Endpoint::of(Tool::Claude, run, agent_id),
        }
    }

    pub(crate) fn endpoint(&self) -> &Endpoint {
        &self.endpoint
    }

    /// Holds this agent's lane starts and ends for a terminal or a move.
    pub(crate) fn hold(&self) -> Arc<Mutex<()>> {
        STARTS
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .entry(self.endpoint.socket().to_owned())
            .or_default()
            .clone()
    }

    /// The lane, if one is there for this conversation, started as this turn would start
    /// Claude Code, and free: what it says of itself.
    pub(crate) fn warm(&self, fit: &Fit) -> Option<Snapshot> {
        let state: Snapshot = self.endpoint.ask(&Request::Inspect).ok()?;
        (state.thread == fit.session
            && state.options == fit.digest
            && state.available
            && state.claim.is_none())
        .then_some(state)
    }

    /// Starts a lane for the conversation `fit` names, with what `order` (a turn's, before it
    /// was wrapped for its own process) starts Claude Code with: `--session-id` for a
    /// conversation the lane's first turn opens, `--resume` for one that is there. A lane that
    /// runs otherwise is retired first, while idle; one there as wanted is left. `None`: nothing
    /// new started.
    pub(crate) fn start(
        &self,
        launcher: &crate::launcher::Launcher,
        run: &Path,
        order: &ShimOrder,
        fit: &Fit,
        mcp: &Mcp,
        login: &Login,
    ) -> io::Result<Option<Child>> {
        let settings = Settings::read();
        let place = self.endpoint.place(
            run,
            Some(&fit.session),
            &fit.digest,
            |_| false,
            settings.room(),
            false,
        )?;
        // Kept: the lane wanted is there. Full: every place runs work, so none is started and
        // the agent's turns run in processes of their own until a place is free (issue 786).
        if matches!(place, Place::Kept | Place::Full) {
            return Ok(None);
        }
        let mut lane_order = order.clone();
        lane_order.stdin_file = self.endpoint.socket().with_extension("stdin");
        let owned: Vec<PathBuf> = crate::mcp::configure(&mut lane_order, mcp.0, mcp.1.as_deref())
            .map(crate::mcp::ScopedConfig::keep)
            .into_iter()
            .collect();
        let dir = threads(run);
        std::fs::create_dir_all(&dir)?;
        let options = json!({
            own_key::TOOL_PROGRAM: lane_order.program,
            own_key::ARGS: lane_order.args,
            own_key::CWD: lane_order.cwd,
            own_key::SESSION: fit.session,
            own_key::OWNED_FILES: owned,
            key::THREAD_LOCK: clarp_shim::lane::thread_lock(&dir, &fit.session),
            key::LOGIN_LOCK: login.lock,
            key::LOGIN_FILE: login.file,
            own_key::CREDENTIALS_FILE: login.credentials,
            key::IDLE_MS: settings.idle_ms,
            key::TURN_FILE: self.endpoint.turn_file(run),
            key::OPTIONS_DIGEST: fit.digest,
        });
        let file = run.join(format!("claude-lane-{}.json", crate::ids::hex(8)));
        let started = self.spawn(launcher, run, &lane_order, &options, &file);
        let _ = std::fs::remove_file(file);
        if started.is_err() {
            for owned in &owned {
                let _ = std::fs::remove_file(owned);
            }
        }
        started.map(Some)
    }

    fn spawn(
        &self,
        launcher: &crate::launcher::Launcher,
        run: &Path,
        order: &ShimOrder,
        options: &Value,
        file: &Path,
    ) -> io::Result<Child> {
        let written = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(file)?;
        serde_json::to_writer(written, options).map_err(io::Error::other)?;
        let mut command = launcher.claude_lane_command(self.endpoint.socket(), file, &order.cwd);
        command
            .envs(lane_env(order))
            .env_remove(TURN_ENV)
            .env(TURN_FILE_ENV, self.endpoint.turn_file(run));
        let mut child = command.spawn()?;
        let mut ready = String::new();
        if let Some(stdout) = child.stdout.take() {
            BufReader::new(stdout).read_line(&mut ready)?;
        }
        if ready.trim() == "ready" {
            Ok(child)
        } else {
            let _ = child.wait();
            Err(io::Error::other("the Claude lane did not start"))
        }
    }

    /// Gives the lane this attempt. `token` is the turn's identity for the Clarp tools it calls.
    /// A refusal for a changed login has the kind `PermissionDenied`: the lane has left, and
    /// nothing of the attempt reached Claude Code.
    pub(crate) fn submit(&self, order: &ShimOrder, token: Option<String>) -> io::Result<Snapshot> {
        let text = std::fs::read_to_string(&order.stdin_file)?;
        self.endpoint.ask(&Request::Prompt {
            claim: Claim {
                turn: order.turn.0,
                attempt: order.attempt,
                spool: order.spool.clone(),
                socket: order.socket.clone(),
            },
            text,
            token,
        })
    }

    /// Ask an idle lane to leave, and wait until it has.
    pub(crate) fn retire(&self) -> io::Result<()> {
        self.endpoint.retire()
    }

    /// The same, waiting at most `bound` for Claude Code to exit. False: it had not let go yet.
    pub(crate) fn retire_within(&self, bound: std::time::Duration) -> io::Result<bool> {
        self.endpoint.retire_within(bound)
    }
}

/// The order of a turn that ran in a process of its own, as a lane for the next turn starts
/// with it: the conversation is there by now, made by that turn, so the lane resumes it,
/// whatever that turn was started with.
pub(crate) fn resuming(order: &ShimOrder) -> ShimOrder {
    let mut order = order.clone();
    for arg in &mut order.args {
        if arg == "--session-id" {
            "--resume".clone_into(arg);
        }
    }
    order
}

/// The guard of a held start, kept while a lane starts or is ended for a terminal or a move.
pub(crate) fn held(hold: &Mutex<()>) -> MutexGuard<'_, ()> {
    hold.lock().unwrap_or_else(PoisonError::into_inner)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::{digest, fits, guard_turn, resuming, without_session};
    use clarp_actor::ShimOrder;
    use clarp_kernel::turn::TurnId;
    use std::path::PathBuf;

    fn order(args: &[&str]) -> ShimOrder {
        ShimOrder {
            before_stop: Some(("SIGINT", 5_000)),
            opening: None,
            turn: TurnId(3),
            attempt: 1,
            spool: PathBuf::from("/run/t3.spool"),
            socket: PathBuf::from("/run/t3.sock"),
            stdin_file: PathBuf::from("/run/t3.stdin"),
            prompt_as_file: false,
            streams_input: true,
            named_session: None,
            model: Some("sonnet".into()),
            effort: None,
            cwd: PathBuf::from("/w"),
            program: "claude".into(),
            args: args.iter().map(|arg| (*arg).to_owned()).collect(),
            env: vec![("CLARP_PROVIDER_TURN".into(), "token-3".into())],
        }
    }

    const NONE: super::Mcp = (true, None);

    const ARGS: [&str; 6] = [
        "-p",
        "--model",
        "sonnet",
        "--resume",
        "s-1",
        "--replay-user-messages",
    ];

    #[test]
    fn a_turn_fits_a_lane_of_its_conversation() {
        let fit = fits(&order(&ARGS), &NONE).unwrap();
        assert_eq!(fit.session, "s-1");
        let new = fits(
            &order(&["-p", "--session-id", "s-2", "--replay-user-messages"]),
            &NONE,
        )
        .unwrap();
        assert_eq!(new.session, "s-2");
    }

    #[test]
    fn a_turn_a_kept_process_cannot_run_keeps_its_own() {
        // A Claude Code that would not say what it read.
        assert!(fits(&order(&["-p", "--resume", "s-1"]), &NONE).is_none());
        // A takeover or rewind.
        let mut args = ARGS.to_vec();
        args.extend(["--resume-session-at", "m", "--resume-drops-turn", "d"]);
        assert!(fits(&order(&args), &NONE).is_none());
        // A turn that only reads.
        let mut reads = order(&ARGS);
        reads
            .env
            .push((clarp_actor::fork::FORK_ENV.into(), "1".into()));
        assert!(fits(&reads, &NONE).is_none());
        // One whose input closes with its first message.
        let mut closed = order(&ARGS);
        closed.streams_input = false;
        assert!(fits(&closed, &NONE).is_none());
    }

    #[test]
    fn two_turns_of_one_agent_share_a_digest_and_a_change_of_model_or_servers_does_not() {
        let mcp = (true, Some(vec!["a".to_owned()]));
        let one = order(&ARGS);
        let mut two = order(&ARGS);
        two.turn = TurnId(4);
        two.env = vec![("CLARP_PROVIDER_TURN".into(), "token-4".into())];
        let none = serde_json::Value::Null;
        assert_eq!(digest(&one, &mcp, &none), digest(&two, &mcp, &none));
        let other_model = order(&["-p", "--model", "opus", "--resume", "s-1"]);
        assert_ne!(digest(&one, &mcp, &none), digest(&other_model, &mcp, &none));
        assert_ne!(
            digest(&one, &mcp, &none),
            digest(&one, &(true, None), &none)
        );
        // A server edited under the same name is another lane's.
        let server = |command: &str| serde_json::json!({"a": {"command": command}});
        assert_ne!(
            digest(&one, &mcp, &server("old")),
            digest(&one, &mcp, &server("new"))
        );
        let mut moved = order(&ARGS);
        moved.env.push(("host_address".into(), "other".into()));
        assert_ne!(digest(&one, &mcp, &none), digest(&moved, &mcp, &none));
    }

    #[test]
    fn a_lane_after_a_turn_of_its_own_resumes_and_a_first_turns_lane_opens_its_conversation() {
        let first = order(&["-p", "--session-id", "s-2", "--replay-user-messages"]);
        // After that turn's own process, the lane resumes what it made.
        assert_eq!(
            resuming(&first).args,
            ["-p", "--resume", "s-2", "--replay-user-messages"]
        );
        // Started as the turn itself, it opens it: the order is the turn's own.
        assert_eq!(first.args[1], "--session-id");
        // Both are one lane: the conversation's flags are not in the digest.
        let fit = fits(&first, &NONE).unwrap();
        assert_eq!(fits(&resuming(&first), &NONE).unwrap().digest, fit.digest);
    }

    #[test]
    fn the_conversations_flags_are_not_what_a_lane_is_started_with() {
        let args: Vec<String> = ARGS.iter().map(|arg| (*arg).to_owned()).collect();
        assert_eq!(
            without_session(&args),
            ["-p", "--model", "sonnet", "--replay-user-messages"]
        );
    }

    #[test]
    fn a_turn_of_its_own_process_takes_its_conversations_lock_shared() {
        let run = std::env::temp_dir().join(format!("claude-lane-guard-{}", std::process::id()));
        let mut turn = order(&ARGS);
        guard_turn(&mut turn, &run, "s-1").unwrap();
        assert_eq!(turn.program, "flock");
        assert_eq!(&turn.args[..3], ["--shared", "--nonblock", "--no-fork"]);
        assert!(turn.args[3].starts_with(run.join("claude-threads").to_str().unwrap()));
        assert_eq!(turn.args[4], "claude");
        std::fs::remove_dir_all(&run).unwrap();
    }
}
