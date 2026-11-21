//! The accounts of the agents' tools, on a Host that runs the owner's selector itself.
//!
//! Where this Host is alone on its machine (`clarp_kernel::recovery::Accounts::Select`),
//! nobody else puts another login in place when an account is out. The owner has a program
//! that does, one for each tool: the selector. It changes the login the whole machine uses,
//! so nothing of the tool may run while it does. What is done with the turns of a tool then
//! is the kernel's to say (`clarp_kernel::accounts::Pool`, which is the Python Host's
//! coordinator); this is what tells it and does what it says: it reaches the agents, keeps
//! the timers, and runs the selector.
//!
//! The selector is run as a program of its own, without a shell, given the models of the
//! waiting turns on its standard input, and has five minutes. Of what it prints nothing is
//! kept and nothing is said: only what kind of answer it was.

use std::io::{Read as _, Write as _};
use std::os::unix::process::CommandExt as _;
use std::process::Stdio;
use std::sync::{Arc, Mutex, OnceLock, PoisonError};
use std::time::{Duration, Instant};

use clarp_actor::Pooled;
use clarp_kernel::accounts::{self, Effect, Pool, Starting, Timer, Verdict};
use clarp_kernel::turn::TurnId;
use clarp_ports::clock::Clock;

use crate::fleet::FleetHandle;

/// How long the owner's selector may take (the Python Host's bound).
const SELECTOR_MOST: Duration = Duration::from_secs(300);

/// The pool of one tool's accounts, and what carries out what it wants done.
pub struct Service {
    tool: &'static str,
    /// The owner's selector for the tool: a program and its arguments.
    command: Vec<String>,
    login_lock: Option<std::path::PathBuf>,
    pool: Mutex<Pool>,
    clock: Arc<dyn Clock + Send + Sync>,
    /// The way to the agents, once there are agents.
    fleet: OnceLock<FleetHandle>,
    /// How long the selector may take.
    selector_most: Duration,
}

impl Service {
    pub fn new(
        tool: &'static str,
        command: Vec<String>,
        clock: Arc<dyn Clock + Send + Sync>,
    ) -> Arc<Self> {
        Self::with_lock(tool, command, clock, None)
    }

    pub fn with_lock(
        tool: &'static str,
        command: Vec<String>,
        clock: Arc<dyn Clock + Send + Sync>,
        login_lock: Option<std::path::PathBuf>,
    ) -> Arc<Self> {
        Arc::new(Self {
            login_lock,
            tool,
            command,
            pool: Mutex::new(Pool::new()),
            clock,
            fleet: OnceLock::new(),
            selector_most: SELECTOR_MOST,
        })
    }

    /// The tool whose accounts these are.
    pub fn tool(&self) -> &'static str {
        self.tool
    }

    /// Hold an OS lock in the tool process. It survives a Host restart with the shim.
    pub fn guard_turn(&self, order: &mut clarp_actor::ShimOrder) {
        let Some(path) = &self.login_lock else { return };
        let program = std::mem::replace(&mut order.program, "flock".into());
        let args = std::mem::take(&mut order.args);
        order.args = vec![
            "--shared".into(),
            "--no-fork".into(),
            path.to_string_lossy().into_owned(),
            program,
        ];
        order.args.extend(args);
    }

    /// Provider one-shots hold the same shared lock as a turn for their full call.
    pub async fn use_login(
        app: &crate::edge::App,
        tool: &str,
    ) -> std::io::Result<Option<std::fs::File>> {
        let Some(path) = app
            .pools
            .iter()
            .find(|p| p.tool() == tool)
            .and_then(|p| p.login_lock.clone())
        else {
            return Ok(None);
        };
        tokio::task::spawn_blocking(move || {
            let file = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(path)?;
            file.lock_shared()?;
            Ok(Some(file))
        })
        .await
        .map_err(std::io::Error::other)?
    }

    pub async fn switch_lock(&self) -> Option<std::fs::File> {
        let path = self.login_lock.clone()?;
        tokio::task::spawn_blocking(move || {
            let file = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(path)
                .ok()?;
            file.lock().ok()?;
            Some(file)
        })
        .await
        .ok()
        .flatten()
    }

    pub fn available(self: &Arc<Self>, serves: impl Fn(&str) -> bool) {
        self.told(|pool, now| ((), pool.login_available(serves, now)));
    }

    /// Gives the pool the way to the agents.
    pub fn attach(&self, fleet: FleetHandle) {
        // Attached once, at the start; a second attach (none is made) keeps the first.
        self.fleet.get_or_init(|| fleet);
    }

    /// The pool as one agent's turn sees it.
    pub fn of(self: &Arc<Self>, agent: &str) -> Arc<dyn Pooled> {
        Arc::new(OfAgent {
            service: self.clone(),
            agent: agent.to_string(),
        })
    }

    /// Tells the pool something and does what it wants done about it, after the pool is let
    /// go of: nothing that is done waits for the pool, and the pool waits for nothing.
    fn told<T>(self: &Arc<Self>, tell: impl FnOnce(&mut Pool, i64) -> (T, Vec<Effect>)) -> T {
        let now = self.clock.now_ms();
        let (answer, effects) = {
            let mut pool = self.pool.lock().unwrap_or_else(PoisonError::into_inner);
            tell(&mut pool, now)
        };
        self.carry_out(effects);
        answer
    }

    /// An attempt of an agent's turn is about to be launched. Whether it has to wait for an
    /// account instead: then it is launched when the pool takes it up.
    pub fn starting(self: &Arc<Self>, agent: &str, turn: TurnId, model: &str) -> bool {
        let trace = turn.0.to_string();
        self.told(|pool, now| {
            let starting = Starting {
                agent,
                trace: &trace,
                model,
                holds: true,
                ends: true,
            };
            pool.register(starting, now)
        })
    }

    /// An attempt of an agent's turn failed on its account. Whether the turn waits.
    /// `plan`: the provider said that the account does not offer the turn's model.
    pub fn failed(self: &Arc<Self>, agent: &str, turn: TurnId, plan: bool) -> bool {
        let trace = turn.0.to_string();
        self.told(|pool, now| pool.request(agent, &trace, true, plan, now))
    }

    /// Nothing of the agent's turn runs any more.
    pub fn stopped(self: &Arc<Self>, agent: &str) {
        self.told(|pool, now| ((), pool.stopped(agent, now)));
    }

    /// The agent's turn is over.
    pub fn over(self: &Arc<Self>, agent: &str, turn: TurnId) {
        let trace = turn.0.to_string();
        self.told(|pool, now| ((), pool.discard(agent, &trace, now)));
    }

    /// The agent's turn is no longer the agent's to finish: its user stopped it, or the
    /// agent is gone.
    pub fn disowned(self: &Arc<Self>, agent: &str) {
        self.told(|pool, now| ((), pool.disown(agent, now)));
    }

    /// A Host that starts found turns that wait for an account: the recovery they waited in
    /// is taken up, from when the first of them began to wait.
    pub fn carried(self: &Arc<Self>, since_ms: i64, agents: &[String]) {
        self.told(|pool, now| ((), pool.carried(true, since_ms, agents, now).1));
    }

    /// Whether the owner has a selector for this tool: a recovery can be asked of the pool.
    pub fn has_selector(&self) -> bool {
        !self.command.is_empty()
    }

    /// A recovery the Quota keeper asks for: the first turn the pool knows that is still its
    /// agent's is taken up as if it had failed on its account (the runtime's
    /// `runtime_recover`), and what that wants done is done. `None`: no such turn; else
    /// whether the pool took it.
    pub fn recover(self: &Arc<Self>) -> Option<bool> {
        let selector = self.has_selector();
        self.told(|pool, now| match pool.first_owned() {
            None => (None, Vec::new()),
            Some((agent, trace)) => {
                let (taken, effects) = pool.request(&agent, &trace, selector, false, now);
                (Some(taken), effects)
            }
        })
    }

    /// Whether a recovery is under way, and whose turns wait.
    pub fn waiting(&self) -> (bool, Vec<String>) {
        let pool = self.pool.lock().unwrap_or_else(PoisonError::into_inner);
        (pool.recovering(), pool.waiting())
    }

    fn carry_out(self: &Arc<Self>, effects: Vec<Effect>) {
        for effect in effects {
            match effect {
                // A turn that waits says so by itself: a parked turn by what it is, a
                // launch that is held by whoever holds it.
                Effect::Pause { .. } => {}
                // One that waits for its model while the rest of the pool works says which.
                Effect::Hold { agent } => {
                    if let Some(reached) = self.agent(&agent) {
                        reached.account_held();
                    }
                }
                Effect::Stop { agent } => match self.agent(&agent) {
                    Some(reached) => reached.stop_for_an_account(),
                    // An agent that is gone runs nothing.
                    None => self.stopped(&agent),
                },
                Effect::Resume { agent, .. } => {
                    if let Some(reached) = self.agent(&agent) {
                        reached.account_back();
                    }
                }
                // No account's plan offers the turn's model: it ends with what the provider
                // said of its own.
                Effect::End { agent } => {
                    if let Some(reached) = self.agent(&agent) {
                        reached.account_end();
                    }
                }
                Effect::Ask { models } => self.ask(models),
                Effect::Timer { timer, after_ms } => self.later(timer, after_ms),
            }
        }
    }

    fn agent(&self, agent_id: &str) -> Option<crate::agent::AgentHandle> {
        let directory = self.fleet.get()?.directory();
        let mut everyone = directory.members.iter().chain(&directory.janitors);
        everyone
            .find(|member| member.record.agent_id == agent_id)
            .map(|member| member.agent.clone())
    }

    fn later(self: &Arc<Self>, timer: Timer, after_ms: i64) {
        let service = self.clone();
        let after = Duration::from_millis(u64::try_from(after_ms).unwrap_or(0));
        let spawned = std::thread::Builder::new()
            .name(format!("{}-accounts-timer", self.tool))
            .spawn(move || {
                std::thread::sleep(after);
                service.told(|pool, now| ((), pool.fired(timer, now)));
            });
        if let Err(error) = spawned {
            eprintln!(
                "clarp-host: the {} accounts: a timer could not be set: {error}",
                self.tool
            );
        }
    }

    fn ask(self: &Arc<Self>, models: Vec<String>) {
        let service = self.clone();
        let spawned = std::thread::Builder::new()
            .name(format!("{}-accounts-selector", self.tool))
            .spawn(move || {
                let command = if let Some(lock) = &service.login_lock
                    && !service.command.is_empty()
                {
                    let mut args = vec![
                        "flock".into(),
                        "--exclusive".into(),
                        "--no-fork".into(),
                        lock.to_string_lossy().into_owned(),
                    ];
                    args.extend(service.command.iter().cloned());
                    args
                } else {
                    service.command.clone()
                };
                let verdict = selected(&command, &models, service.selector_most);
                let said = match &verdict {
                    Verdict::All => "an account serves",
                    Verdict::Only(_) => "the account now active serves some of the models",
                    Verdict::NoAccount => "no account serves",
                    Verdict::NoAnswer => "no answer",
                    Verdict::Failed => "it could not be run",
                };
                eprintln!(
                    "clarp-host: the {} accounts: the selector was asked for {} model(s): {said}",
                    service.tool,
                    models.len()
                );
                service.told(|pool, now| ((), pool.answered(verdict, now)));
            });
        if spawned.is_err() {
            // Nobody asked: the pool is told that the selector could not be run, and asks
            // again later.
            self.told(|pool, now| ((), pool.answered(Verdict::Failed, now)));
        }
    }
}

/// Runs the owner's selector for these models and reads its answer.
fn selected(command: &[String], models: &[String], most: Duration) -> Verdict {
    let Some((program, arguments)) = command.split_first() else {
        return Verdict::Failed;
    };
    // In a group of its own, so that what it started goes with it when it takes too long.
    let started = crate::child::command(program)
        .args(arguments)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn();
    let Ok(mut selector) = started else {
        return Verdict::Failed;
    };
    if let Some(mut given) = selector.stdin.take() {
        // A selector that does not read what it is given still answers.
        let _ = given.write_all(accounts::asked(models).as_bytes());
    }
    let printed = selector.stdout.take().map(|mut out| {
        std::thread::spawn(move || {
            let mut text = String::new();
            let _ = out.read_to_string(&mut text);
            text
        })
    });
    let began = Instant::now();
    let exit = loop {
        match selector.try_wait() {
            Ok(Some(status)) => break status.code(),
            Ok(None) if began.elapsed() < most => std::thread::sleep(Duration::from_millis(50)),
            _ => {
                // Too long, or it cannot be waited for: it is ended, with what it started.
                let group = format!("-{}", selector.id());
                let _ = crate::child::command("kill")
                    .args(["-KILL", "--", &group])
                    .status();
                let _ = selector.kill();
                let _ = selector.wait();
                break None;
            }
        }
    };
    let printed = printed
        .and_then(|reader| reader.join().ok())
        .unwrap_or_default();
    accounts::read(exit, &printed, models)
}

/// The pool, as the turn of one agent tells it things.
struct OfAgent {
    service: Arc<Service>,
    agent: String,
}

impl Pooled for OfAgent {
    fn failed(&self, turn: TurnId, plan: bool, _now_ms: i64) -> bool {
        self.service.failed(&self.agent, turn, plan)
    }

    fn stopped(&self, _now_ms: i64) {
        self.service.stopped(&self.agent);
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn script(dir: &std::path::Path, name: &str, body: &str) -> Vec<String> {
        let path = dir.join(name);
        std::fs::write(&path, body).unwrap();
        vec!["/bin/sh".to_string(), path.display().to_string()]
    }

    #[test]
    fn a_selector_is_given_the_models_and_its_answer_is_read() {
        let dir = std::env::temp_dir().join(format!("clarp-pool-selector-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let models = ["opus".to_string(), "sonnet".to_string()];
        let most = Duration::from_secs(20);
        // It is given the models as the Python Host gives them, and says that one is served.
        let given = dir.join("given");
        let body = format!(
            "cat > {}\nprintf '%s' '{{\"available\": true, \"models\": [\"sonnet\"]}}'\n",
            given.display()
        );
        let verdict = selected(&script(&dir, "some.sh", &body), &models, most);
        let served = ["sonnet".to_string()].into_iter().collect();
        assert_eq!(verdict, Verdict::Only(served));
        assert_eq!(
            std::fs::read_to_string(&given).unwrap(),
            r#"{"models": ["opus", "sonnet"]}"#
        );
        // One that does not read what it is given, and says yes; one that says no; one that
        // ends badly; one that prints what cannot be read.
        let yes = script(&dir, "yes.sh", "printf '%s' '{\"available\": true}'\n");
        assert_eq!(selected(&yes, &models, most), Verdict::All);
        let no = script(
            &dir,
            "no.sh",
            "cat > /dev/null\nprintf '%s' '{\"available\": false}'\n",
        );
        assert_eq!(selected(&no, &models, most), Verdict::NoAccount);
        let bad = script(
            &dir,
            "bad.sh",
            "printf '%s' '{\"available\": true}'\nexit 3\n",
        );
        assert_eq!(selected(&bad, &models, most), Verdict::NoAnswer);
        let odd = script(&dir, "odd.sh", "echo not json\n");
        assert_eq!(selected(&odd, &models, most), Verdict::NoAnswer);
        // A program that is not there, and no program at all, could not be run.
        let absent = vec![dir.join("absent").display().to_string()];
        assert_eq!(selected(&absent, &models, most), Verdict::Failed);
        assert_eq!(selected(&[], &models, most), Verdict::Failed);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_selector_that_takes_too_long_is_ended_and_said_nothing() {
        let dir = std::env::temp_dir().join(format!("clarp-pool-slow-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        // It starts something of its own that outlives it, and waits for that.
        let slow = script(
            &dir,
            "slow.sh",
            "sleep 30 &\nwait\nprintf '%s' '{\"available\": true}'\n",
        );
        let began = Instant::now();
        let verdict = selected(&slow, &["opus".to_string()], Duration::from_millis(300));
        assert_eq!(verdict, Verdict::NoAnswer);
        assert!(
            began.elapsed() < Duration::from_secs(10),
            "it was waited for to its end"
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
