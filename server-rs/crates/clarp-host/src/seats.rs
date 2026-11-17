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
//! (`clarp_kernel::desk::of_this_host`).

use std::process::Stdio;
use std::time::Duration;

use clarp_kernel::desk::{self, Answer, Answers, Asked, Failed, Outcome};
use clarp_kernel::seats::Seats;
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
/// This Host has no switcher of seats of its own: none is on.
const SWITCHER_ON: bool = false;

/// What is kept with the fleet: the seats as the strict answer tells them, and the desk.
#[derive(Default, Serialize, Deserialize)]
struct Kept {
    #[serde(default)]
    seats: Seats,
    #[serde(default)]
    desk: desk::Desk,
}

#[derive(Default)]
struct State {
    /// What is kept: read from the fleet once, and written back with every read.
    kept: Option<Kept>,
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
}

impl Desk {
    pub fn new(tool: &str, renews: bool) -> Self {
        Self {
            tool: tool.to_string(),
            renews,
            state: tokio::sync::Mutex::default(),
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
    async fn read(&self, app: &App) -> Outcome {
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
            if began_ms < kept.desk.next_read_at(last_attempt_ms) {
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
            tool: &member.record.backend,
            working: view.status.live.is_some(),
            tool_session: view.status.backend_session.as_deref(),
        })
        .collect();
    desk::of_this_host(of_desk, list, &agents)
}

/// `GET /hotseat/status`: how the account seats stand, as the tool's list last said them.
/// Nothing is read for it.
pub async fn status(app: &App) -> Value {
    let now = app.clock.now_ms();
    let mut state = app.seats.state.lock().await;
    match state.kept(app).await {
        Some(kept) => kept.seats.served(SWITCHER_ON, now),
        None => Seats::default().served(SWITCHER_ON, now),
    }
}

/// `GET /hotseat/desk`: the desk as it is kept; with `refresh`, after a live read where one
/// is allowed. The read runs to its end whether or not whoever asked for it is still there.
pub async fn asked(app: &App, refresh: bool) -> Value {
    let outcome = if refresh {
        let reader = app.clone();
        let read = tokio::spawn(async move { reader.seats.read(&reader).await });
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
    match state.kept(app).await {
        Some(kept) => kept
            .desk
            .served(&kept.seats.served(SWITCHER_ON, now), &asked),
        None => {
            let nothing = Seats::default().served(SWITCHER_ON, now);
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
