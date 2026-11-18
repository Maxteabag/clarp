//! The desk of the machine's account seats: the owner's own tool is asked, and what it
//! answered is kept and served.
//!
//! The owner has a tool that knows every saved account of his agents' tools and how much of
//! each is left (Hotseat). This Host runs it when an app asks for a new reading of the desk
//! (`GET /hotseat/desk?refresh=1`) and at no other time: its three askings at once, each in a
//! group of its own and with its time; one read at a time, and no sooner after the last than
//! the kernel's desk allows (`clarp_kernel::desk`). Whoever asks for a read while one is
//! under way waits for that one. What the tool answered is made into the desk, kept with the
//! fleet, and served as it was read; its list is also what the strict answer of
//! `GET /hotseat/status` is made of (`clarp_kernel::seats`).
//!
//! A Host is told its owner's tool (`--seats-tool`). One that was told none runs nothing and
//! says that the tool is not installed: no Host that is started bare, for a test or to try
//! something, ever asks the providers about the owner's accounts.
//!
//! Reading is all this does. On a machine with another Host, that Host keeps the logins: the
//! tool is then told to renew none as it reads, and this Host moves no seat
//! (`docs/differences.md`).
//!
//! What the tool says of a Clarp Host's agents it has from the Host it is bridged to, which
//! on a machine with a Python Host is that one. So of agents this Host says its own word
//! (`clarp_kernel::desk::of_this_host`): its agents, counted, and those of them whose work
//! stopped: a turn that failed for a usage limit with none since, a turn that waits for an
//! account, a queue that is held with turns waiting.
//!
//! From the desk an app asks that such work go on (`POST /hotseat/resume`,
//! `clarp_kernel::resume`): an agent the kept desk lists as stopped is sent one prompt for
//! that stop, under an id made of the agent and the stop, so that asking twice sends once;
//! a held queue is let go; and for a session that is no Host's the app is given the command
//! a person types. A turn that waits for an account is not sent a prompt: it is tried at
//! once on the account that is there, which is all a person who asks for it can mean.

use serde_json::json;
use std::collections::BTreeMap;
use std::process::Stdio;
mod switching;
pub(crate) mod automatic;
pub use automatic::start as start_switcher;
use std::time::Duration;

use clarp_kernel::conversation::Message;
use clarp_kernel::desk::{self, Answer, Answers, Asked, Cause, Failed, Outcome};
use clarp_kernel::recovery::Accounts;
use clarp_kernel::resume::{self, Continued, Delivery, Standing, Wanted};
use clarp_kernel::seats::Seats;
use clarp_protocol::requests::SendRequest;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::io::AsyncReadExt as _;
use tokio::sync::watch;

use crate::edge::App;

/// The kind of thing this is, among what is left with the fleet, and the one key it has.
const KIND: &str = "seats";
const KEPT: &str = "desk";
/// No more of what the tool prints for one asking is read than this.
const MOST_PRINTED: u64 = 8 * 1024 * 1024;
/// How long whoever asked for a read waits for the one that is under way: the longest
/// asking and five seconds, as the Python Host waits.
const WAITED_FOR: Duration = Duration::from_secs(desk::DESK_ASKED.1 + 5);
/// What the tool is told so that it renews no login as it reads.
const RENEW_NOTHING: (&str, &str) = ("HOTSEAT_NO_REFRESH", "1");
/// What is kept with the fleet: the seats as the strict answer tells them, and the desk.
#[derive(Default, Serialize, Deserialize)]
struct Kept {
    #[serde(default)]
    seats: Seats,
    #[serde(default)]
    desk: desk::Desk,
    #[serde(default)]
    switches: BTreeMap<String, Value>,
    #[serde(default)]
    last_switch: BTreeMap<String, Value>,
    #[serde(default)]
    notifications: BTreeMap<String, String>,
}

#[derive(Default)]
struct State {
    /// What is kept: read from the fleet once, and written back with every read.
    kept: Option<Kept>,
    running: BTreeMap<String, Value>,
    /// When this Host, since it started, last began a read.
    last_attempt_ms: i64,
    /// The read that is under way, for whoever else asks for one: it says how it ended.
    under_way: Option<watch::Receiver<Option<bool>>>,
}

impl State {
    /// What is kept: read from the fleet the first time. `None` where the fleet cannot be
    /// asked: nothing is known then, and nothing may be written over.
    async fn kept(&mut self, app: &App) -> Option<&mut Kept> {
        if self.kept.is_none() {
            let written = app.fleet.kept(KIND, KEPT.to_string()).await.ok()?;
            let read = written.and_then(|bytes| serde_json::from_slice::<Kept>(&bytes).ok());
            let mut kept = read.unwrap_or_default();
            // Whose reading a kept reading is, is told by a digest under a key of this
            // Host's own, drawn once.
            if !kept.seats.has_salt() {
                kept.seats = Seats::with_salt(&crate::ids::hex(32));
            }
            self.kept = Some(kept);
        }
        self.kept.as_mut()
    }
}

/// The desk of the seats, as one Host has it.
pub struct Desk {
    /// The owner's tool: a program's name or its path. Empty: this Host was told none.
    tool: String,
    /// Whether the tool may renew a login as it reads: on a Host that keeps the logins.
    renews: bool,
    state: tokio::sync::Mutex<State>,
    switches: [tokio::sync::Mutex<()>; 2],
}

impl Desk {
    pub fn new(tool: &str, renews: bool) -> Self {
        Self {
            tool: tool.to_string(),
            renews,
            state: tokio::sync::Mutex::default(),
            switches: Default::default(),
        }
    }

    /// Whether this Host was told its owner's tool: one that was not has no desk to show.
    pub fn told(&self) -> bool {
        !self.tool.is_empty()
    }

    /// One asking of the tool: run without a shell, in a group of its own, given nothing,
    /// and read for at most its time; then it is ended, with what it started. The Host's
    /// own secrets stay with the Host.
    async fn asking(&self, (args, most): (&[&str], u64)) -> Answer {
        self.command(args, most, None, false).await
    }

    async fn command(
        &self,
        args: &[&str],
        most: u64,
        lock: Option<&std::fs::File>,
        exit_only: bool,
    ) -> Answer {
        // A Host that was told no tool has no program to start: that fails as one that is
        // not there does.
        let mut tool = tokio::process::Command::new(&self.tool);
        tool.args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .process_group(0)
            .kill_on_drop(true);
        for name in crate::OWN_SECRETS {
            tool.env_remove(name);
        }
        if let Some(lock) = lock {
            // The child retains the machine-wide lock even if its Host exits during the switch.
            tool.stdin(lock.try_clone().map_err(|_| Failed::DidNotRun)?);
        }
        if !self.renews {
            tool.env(RENEW_NOTHING.0, RENEW_NOTHING.1);
        }
        let Ok(mut running) = tool.spawn() else {
            return Err(Failed::DidNotRun);
        };
        let group = running.id();
        let printed = running.stdout.take();
        let to_its_end = async {
            let mut bytes = Vec::new();
            if let Some(printed) = printed {
                // What is beyond the most is not read: the tool finds its pipe closed.
                let _ = printed.take(MOST_PRINTED).read_to_end(&mut bytes).await;
            }
            (running.wait().await, bytes)
        };
        let ended = tokio::time::timeout(Duration::from_secs(most), to_its_end).await;
        match ended {
            Ok((Ok(ended), _)) if exit_only && ended.success() => Ok(Value::Bool(true)),
            Ok((Ok(ended), bytes)) => desk::answer(ended.code(), &String::from_utf8_lossy(&bytes)),
            Ok((Err(_), _)) => Err(Failed::DidNotRun),
            Err(_) => {
                if let Some(group) = group {
                    let group = format!("-{group}");
                    let mut kill = tokio::process::Command::new("kill");
                    let _ = kill.args(["-KILL", "--", &group]).status().await;
                }
                let _ = running.kill().await;
                Err(Failed::TookTooLong)
            }
        }
    }

    /// A live read of the tool, for whoever asked: the one under way is waited for, none is
    /// begun before one is allowed, and otherwise the tool is asked now.
    async fn read(&self, app: &App, force:bool) -> Outcome {
        let began_ms = app.clock.now_ms();
        let done = {
            let mut state = self.state.lock().await;
            if let Some(mut under_way) = state.under_way.clone() {
                drop(state);
                let ended = under_way.wait_for(|ended| ended.is_some());
                let ended = tokio::time::timeout(WAITED_FOR, ended).await;
                let made = matches!(ended, Ok(Ok(ended)) if *ended == Some(true));
                return if made { Outcome::Read } else { Outcome::Failed };
            }
            let last_attempt_ms = state.last_attempt_ms;
            let Some(kept) = state.kept(app).await else {
                return Outcome::Failed;
            };
            if !force && began_ms < kept.desk.next_read_at(last_attempt_ms) {
                return Outcome::Held;
            }
            state.last_attempt_ms = began_ms;
            let (done, under_way) = watch::channel(None);
            state.under_way = Some(under_way);
            done
        };
        let (of_desk, list, work) = tokio::join!(
            self.asking(desk::DESK_ASKED),
            self.asking(desk::LIST_ASKED),
            self.asking(desk::WORK_ASKED),
        );
        // Of agents this Host says its own word, as they are at the end of the read.
        let agents = match &of_desk {
            Ok(of_desk) => Some(own_word(app, of_desk, list.as_ref().ok())),
            Err(_) => None,
        };
        let answers = Answers {
            desk: of_desk,
            list,
            work,
            agents,
        };
        let mut state = self.state.lock().await;
        let mut made = false;
        if let Some(kept) = state.kept(app).await {
            made = kept.desk.read(&mut kept.seats, answers, began_ms);
            for (provider, record) in &kept.last_switch {
                if record["at"].as_i64().is_some_and(|stamp| stamp >= began_ms)
                    && let Some(alias) = record["to"].as_str()
                {
                    kept.desk.mark_active(provider, alias);
                }
            }
            if let Ok(written) = serde_json::to_vec(kept) {
                let _ = app.fleet.keep(KIND, KEPT.to_string(), Some(written)).await;
            }
        }
        state.under_way = None;
        drop(state);
        let _ = done.send(Some(made));
        if made { Outcome::Read } else { Outcome::Failed }
    }
}

/// What this Host says of its own agents in the place of what the tool says of a Host's:
/// each agent of the roster with the tool it runs, whether it works now, and its tool's own
/// session.
fn own_word(app: &App, of_desk: &Value, list: Option<&Value>) -> Value {
    let directory = app.fleet.directory();
    let roster = directory.members.iter().filter(|member| {
        member.record.deleted_at_ms.is_none() && member.record.archived_at_ms.is_none()
    });
    let views: Vec<_> = roster
        .map(|member| (member.as_ref(), member.agent.view()))
        .collect();
    let agents: Vec<desk::Agent<'_>> = views
        .iter()
        .map(|(member, view)| desk::Agent {
            session: &member.record.session,
            name: name_of(&member.record),
            tool: &member.record.backend,
            model: &member.record.model,
            cwd: &member.record.cwd,
            working: view.status.live.is_some(),
            tool_session: view.status.backend_session.as_deref(),
            stopped: stopped(&view.status),
            waiting: view.status.waiting.len(),
        })
        .collect();
    desk::of_this_host(of_desk, list, &agents, app.clock.now_ms())
}

/// What a person calls an agent.
fn name_of(record: &crate::fleet::AgentRecord) -> &str {
    if record.persona.is_empty() {
        &record.session
    } else {
        &record.persona
    }
}

/// Whether a live turn waits for an account: between two attempts, or before its first.
fn waits_for_an_account(live: &clarp_actor::LiveStatus) -> bool {
    let parked = live.parked.as_ref();
    live.held_for_an_account || parked.is_some_and(|parked| parked.wait.is_for_an_account())
}

/// Why an agent's work stopped and when, where it did and has not gone on since. A turn
/// that runs has not stopped; one that waits for an account has, since it began to wait. An
/// agent at rest has stopped where its queue is held with turns waiting, or where its last
/// turn failed for a usage limit.
fn stopped(status: &clarp_actor::Status) -> Option<(Cause, i64)> {
    if let Some(live) = &status.live {
        let since = match &live.parked {
            Some(parked) if parked.wait.is_for_an_account() => parked.since_ms,
            _ => live.started_at_ms,
        };
        return waits_for_an_account(live).then_some((Cause::WaitsForAnAccount, since));
    }
    let last = status.last.as_ref();
    if status.paused && !status.waiting.is_empty() {
        return Some((Cause::QueueHeld, last.map_or(0, |last| last.ended_at_ms)));
    }
    let last = last?;
    let refused = matches!(
        &last.outcome,
        clarp_kernel::turn::Outcome::Failed { kind, .. } if kind == clarp_kernel::failures::USAGE_LIMIT
    );
    refused.then_some((Cause::UsageLimit, last.ended_at_ms))
}

/// A text of an asking's body, as the Python Host takes one: nothing where there is none.
fn said(asked: &Value, key: &str) -> String {
    match &asked[key] {
        Value::String(said) => said.clone(),
        other if clarp_kernel::python::truthy(other) => clarp_kernel::python::repr(other),
        _ => String::new(),
    }
}

/// `POST /hotseat/switch`: another account is asked to be the one in use. `None`: this Host
/// would have to do it, and that is not built. A Host that does not put the logins of its
/// machine in place does not do it at all: it says so, in the shape of the Python Host's
/// refusals, once the asking is one it could read.
pub async fn switch(app: &App, asked: &Value) -> resume::Answer {
    let read = resume::switch_wanted(&said(asked, "provider"), &said(asked, "alias"));
    let (tool, alias) = match read {
        Ok(wanted) => wanted,
        Err(refusal) => return refusal,
    };
    if app.config.accounts == Accounts::Select {
        return switching::manual(app, tool, alias).await;
    }
    let body = serde_json::json!({
        "ok": false, "error": "logins_kept_elsewhere", "provider": tool, "alias": alias,
        "message": "This Host reads the account seats and moves none. The account in use is \
                    changed where the logins of this machine are kept.",
    });
    (409, body)
}

/// `POST /hotseat/resume`: work that stopped goes on, a held queue is let go, or the command
/// of a session that is no Host's is given. The status and the body.
pub async fn resume(app: &App, asked: &Value) -> resume::Answer {
    let text = |key: &str| said(asked, key);
    let wanted = resume::wanted(
        &text("id"),
        &text("kind"),
        &text("backend"),
        &text("action"),
    );
    let (id, lets_go) = match wanted {
        Wanted::Answered(answer) => return answer,
        Wanted::Continue(id) => (id, false),
        Wanted::LetGo(id) => (id, true),
    };
    // The agent, by its name or its id: one of the roster or a janitor. (One that was
    // removed is in neither.)
    let directory = app.fleet.directory();
    let Some(member) = directory.named(&id).or_else(|| directory.janitor(&id)) else {
        return resume::in_the_way(None).unwrap_or((404, Value::Null));
    };
    let view = member.agent.view();
    let live = view.status.live.as_ref();
    let waits = live.is_some_and(waits_for_an_account);
    let agent = resume::Agent {
        session: &member.record.session,
        agent_id: &member.record.agent_id,
        name: name_of(&member.record),
        archived: member.record.archived_at_ms.is_some(),
        janitor: member.record.is_janitor,
        held: view.status.paused,
        // A turn that waits for an account does not work: it is what is to go on.
        works: live.is_some() && !waits,
    };
    if let Some(answer) = resume::in_the_way(Some(&agent)) {
        return answer;
    }
    let unavailable = || {
        (
            503,
            serde_json::json!({ "ok": false, "error": "agent_unavailable" }),
        )
    };
    if lets_go {
        return match member.agent.resume("user".to_string()).await {
            Ok((_, queue)) => resume::let_go(agent.session, queue.paused, queue.waiting as u64),
            Err(_) => unavailable(),
        };
    }
    // Its stop, as the kept desk lists it, and what has happened since.
    let (stopped_work, observed_at) = {
        let mut state = app.seats.state.lock().await;
        match state.kept(app).await {
            Some(kept) => (kept.desk.stopped_work().cloned(), kept.desk.observed_at()),
            None => (None, None),
        }
    };
    let stopped_work = stopped_work.unwrap_or(Value::Null);
    let stop = resume::stop_of(&agent, &stopped_work);
    let mut already_sent = false;
    if let Some((_, stopped_at)) = stop {
        // The prompt of this stop is in the conversation, or waits in the queue.
        let id = resume::message_id(agent.agent_id, stopped_at);
        let in_the_conversation = member.agent.row(Message::filed(&id).0).await;
        already_sent = matches!(in_the_conversation, Ok(Some(_)));
        if !already_sent && !view.status.waiting.is_empty() {
            let waiting = member.agent.queue().await;
            already_sent =
                waiting.is_ok_and(|(waiting, _)| waiting.iter().any(|one| one.client_id == id));
        }
    }
    let standing = Standing {
        stop,
        observed_at,
        can_send: true,
        already_sent,
        completed_at: view.status.completed_at_ms,
        now: app.clock.now_ms(),
    };
    let (text, message_id) = match resume::continued(&agent, &standing) {
        Continued::Refused(answer) => return answer,
        Continued::Send { text, message_id } => (text, message_id),
    };
    let stopped_at = stop.map_or(0, |(_, at)| at);
    if waits {
        // Its turn waits for an account: it is tried now on the account that is there.
        // Where this Host puts the logins in place itself, its selector takes the turn up.
        if app.config.accounts == Accounts::Select {
            let name = agent.name;
            let why = format!(
                "{name} waits for an account. This Host's selector takes it up when one serves it."
            );
            let body =
                serde_json::json!({ "ok": false, "error": "waiting_for_account", "message": why });
            return (409, body);
        }
        member.agent.login_changed();
        let tried = Delivery {
            ok: true,
            ..Delivery::default()
        };
        return resume::sent(&agent, stopped_at, false, &tried);
    }
    let request = SendRequest {
        session: agent.session.to_string(),
        text,
        client_msg_id: Some(message_id),
        synthesize_audio: Some(false),
        hands_free: Some(false),
        force_session: Some(true),
        // A turn that began between the look above and this send is waited for.
        queue_if_busy: Some(true),
        origin: Some("automation".to_string()),
        ..SendRequest::default()
    };
    match crate::edge::send_own(app, request).await {
        Ok(taken) => {
            let delivered = Delivery {
                ok: true,
                queued: taken.queued == Some(true),
                ..Delivery::default()
            };
            resume::sent(&agent, stopped_at, already_sent, &delivered)
        }
        Err(refused) => {
            let why = format!("{}: {}", refused.status().as_u16(), refused.message());
            let failed = Delivery {
                error: &why,
                ..Delivery::default()
            };
            resume::sent(&agent, stopped_at, already_sent, &failed)
        }
    }
}

/// `GET /hotseat/status`: how the account seats stand, as the tool's list last said them.
/// Nothing is read for it.
pub async fn status(app: &App) -> Value {
    let enabled=automatic::enabled(app).await;
    let now = app.clock.now_ms();
    let mut state = app.seats.state.lock().await;
    match state.kept(app).await {
        Some(kept) => kept.seats.served(enabled, now),
        None => Seats::default().served(enabled, now),
    }
}

/// `GET /hotseat/desk`: the desk as it is kept; with `refresh`, after a live read where one
/// is allowed. The read runs to its end whether or not whoever asked for it is still there.
pub async fn asked(app: &App, refresh: bool) -> Value {
    let enabled=automatic::enabled(app).await;
    let outcome = if refresh {
        let reader = app.clone();
        let read = tokio::spawn(async move { reader.seats.read(&reader,false).await });
        Some(read.await.unwrap_or(Outcome::Failed))
    } else {
        None
    };
    let desk = &app.seats;
    let tool = desk.tool.clone();
    let installed = !tool.is_empty()
        && tokio::task::spawn_blocking(move || crate::logins::installed(&tool))
            .await
            .unwrap_or(false);
    let now = app.clock.now_ms();
    let mut state = desk.state.lock().await;
    let asked = Asked {
        served_at: now,
        installed,
        reading: state.under_way.is_some(),
        outcome,
        last_attempt_ms: state.last_attempt_ms,
    };
    let running = state.running.clone();
    match state.kept(app).await {
        Some(kept) => {
            let mut payload = kept
                .desk
                .served(&kept.seats.served(enabled, now), &asked);
            payload["switches"] = json!(
                ["claude", "codex"]
                    .iter()
                    .filter_map(|tool| running.get(*tool).or_else(|| kept.switches.get(*tool)))
                    .collect::<Vec<_>>()
            );
            payload
        }
        None => {
            let nothing = Seats::default().served(enabled, now);
            desk::Desk::default().served(&nothing, &asked)
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use serde_json::json;

    use super::*;

    /// A program in the place of the owner's tool.
    fn tool(dir: &std::path::Path, name: &str, body: &str, renews: bool) -> Desk {
        use std::os::unix::fs::PermissionsExt;
        let path = dir.join(name);
        std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        Desk::new(path.to_str().unwrap(), renews)
    }

    #[tokio::test]
    async fn an_asking_of_the_tool_is_read_to_its_end_or_ended_at_its_time() {
        let dir = std::env::temp_dir().join(format!("clarp-seats-tool-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        // What it prints is read; it is given what it is asked, and told to renew no login
        // unless this Host keeps the logins.
        let says =
            r#"printf '{"asked": "%s %s", "renew": "%s"}' "$1" "$2" "${HOTSEAT_NO_REFRESH:-yes}""#;
        let reads = tool(&dir, "reads", says, false);
        let said = reads.asking((&["tui", "--snapshot"], 20)).await;
        assert_eq!(said, Ok(json!({ "asked": "tui --snapshot", "renew": "1" })));
        let keeps = tool(&dir, "keeps", says, true);
        let said = keeps.asking((&["list", "--json"], 20)).await;
        assert_eq!(said, Ok(json!({ "asked": "list --json", "renew": "yes" })));
        // One that ends badly, one that prints what cannot be read, one that is not there, and
        // a Host that was told none.
        let bad = tool(&dir, "bad", "printf '{}'\nexit 3", false);
        assert_eq!(bad.asking((&["tui"], 20)).await, Err(Failed::DidNotRun));
        let odd = tool(&dir, "odd", "echo Reading account quotas", false);
        assert_eq!(odd.asking((&["tui"], 20)).await, Err(Failed::NotReadable));
        let absent = Desk::new(dir.join("absent").to_str().unwrap(), false);
        assert_eq!(absent.asking((&["tui"], 20)).await, Err(Failed::DidNotRun));
        assert!(absent.told());
        let untold = Desk::new("", false);
        assert_eq!(untold.asking((&["tui"], 20)).await, Err(Failed::DidNotRun));
        assert!(!untold.told());
        // No more than the most is read of what it prints: what is cut off there is no answer.
        let prints = |bytes: u32| {
            format!(r#"printf '{{"a": "'; head -c {bytes} /dev/zero | tr '\0' a; printf '"}}'"#)
        };
        let much = tool(&dir, "much", &prints(9_000_000), false);
        assert!(much.asking((&["tui"], 20)).await.is_err());
        let enough = tool(&dir, "enough", &prints(7_000_000), false);
        assert!(enough.asking((&["tui"], 20)).await.is_ok());
        // One that takes too long is ended, with what it started: what that would have left
        // behind a little later is not there.
        let left = dir.join("left-behind");
        let lingers = format!(
            "(sleep 2; echo late > {}) &\nwait\nprintf '{{}}'",
            left.display()
        );
        let slow = tool(&dir, "slow", &lingers, false);
        let began = std::time::Instant::now();
        assert_eq!(slow.asking((&["tui"], 1)).await, Err(Failed::TookTooLong));
        assert!(
            began.elapsed() < Duration::from_millis(1_900),
            "it was waited for to its end"
        );
        tokio::time::sleep(Duration::from_millis(1_500)).await;
        assert!(!left.exists(), "what it started went on");
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
