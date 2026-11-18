//! What is left of a tool's account, as the tool itself said it: read, kept and shown.
//!
//! Codex writes how much of each window of its account is used, and when the window begins
//! anew, into its conversation's rollout with every count of tokens. When a turn of Codex
//! ends, the newest such record of the turn's rollout is read here
//! (`clarp_backend::limits`), kept for the tool (`clarp_kernel::quota`), and what follows
//! from it is told to the apps: a warning at four fifths of the five-hour window, a hard
//! limit, and when either is over. An app's usage screen is shown what is kept
//! (`GET /backend-usage`). The optional collectors also write their observations here.
//!
//! An agent's row says of its tool that its account is used up (`backend_quota`) while a
//! window that was said to be all used is fresh and no turn of the tool completed since:
//! advice for whoever is about to write to the agent, and no refusal.
//!
//! A turn that fails for a usage limit is a limit too, of which nothing more is known: it is
//! told as a hard limit and stands until a turn of the tool completes.
//!
//! Claude Code says of its account as it runs: with its answers it prints how much of the
//! five hours and of the week is used and when each begins anew. The newest it said in a turn
//! comes with the turn's end, is read (`clarp_backend::limits::claude`), and is kept as the
//! one word there is of that account (`clarp_kernel::standing`), shown and counted as used
//! up by the Python Host's rule for Claude. Whose account it is, is what Claude Code's own
//! files say of who is logged in, kept as a keyed digest like a login of Codex.
//!
//! Whose account a record is of is told by the login the tool is on when the record is
//! read. The file the tool keeps its login in is read for the account and the token, and of
//! both only a keyed digest is kept (the key is this Host's own, drawn once), so that
//! neither can be read back or looked for in what the Host keeps. A record that was
//! written before the login file was last written is of nobody this Host can name, and is
//! not kept.

use std::collections::BTreeMap;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use std::sync::{Arc, PoisonError};
use std::time::{Duration, UNIX_EPOCH};

use clarp_kernel::conversation::LimitsSaid;
use clarp_kernel::quota::{self, Kept, Login, Shown, Tool};
use clarp_kernel::standing::Standing;
use clarp_ports::clock::Clock;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::fleet::FleetHandle;
use crate::hub::Hub;

/// The kind of thing this is, among what is left with the fleet: a tool's records under the
/// tool's name, and the key that logins are named with under [`KEY`].
const KIND: &str = "quota";
const KEY: &str = "key";
/// How much of a rollout's end is read for its newest record of limits. One is written with
/// every count of tokens: the newest lay within eight thousand bytes of the end in each of
/// 37 conversations of the machine this was written on.
const TAIL: u64 = 1024 * 1024;
/// What is said of where a record was seen.
pub const SEEN_IN: &str = "rollout/token_count";
/// How often it is looked whether an account is still used up.
const LOOKED_EVERY: Duration = Duration::from_secs(300);
/// The tool whose records are read.
pub const CODEX: &str = "codex";
/// The tool that says of its account as it runs, and what is said of where its word is from.
pub const CLAUDE: &str = "claude";
pub const SAID_BY_CLAUDE: &str = "claude-code/rate_limit_event";

/// What a record is kept and told with.
#[derive(Clone, Copy)]
pub struct With<'a> {
    pub fleet: &'a FleetHandle,
    pub hub: &'a Hub,
    /// Which Host this is.
    pub server_id: &'a str,
    /// The home of whoever runs this Host: where the tools keep what is theirs.
    pub home: &'a Path,
    pub now_ms: i64,
}

impl<'a> With<'a> {
    /// With what an app's request has, where the user's home is, and now.
    pub fn of(app: &'a crate::edge::App, home: &'a Path) -> Self {
        Self {
            fleet: &app.fleet,
            hub: &app.hub,
            server_id: &app.server_id,
            home,
            now_ms: app.clock.now_ms(),
        }
    }
}

/// How a turn ended, as far as it says something of its tool's account.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ending {
    /// It completed, then: the account served it.
    Completed(i64),
    /// It failed for a usage limit: the account did not serve it.
    Refused,
    /// Cut off, stopped, or failed for something else: it says nothing of the account.
    Otherwise,
}

/// Which tools' accounts are used up, by the tool's name, with what an agent's row says of
/// its tool then (`backend_quota`). A tool that is not named is not known to be.
pub type UsedUp = BTreeMap<String, Value>;

/// What the tools said of their accounts.
#[derive(Default)]
pub struct Desk {
    /// What is kept: read from the fleet once, and written back with every change. One at a
    /// time reads and changes it.
    known: tokio::sync::Mutex<Known>,
    refresh: tokio::sync::Mutex<()>,
    verified: std::sync::Mutex<BTreeMap<String, ([u8; 32], i64)>>,
    agy_attempt: std::sync::Mutex<Option<i64>>,
    /// Which accounts were used up when that was last looked at, and which tools were
    /// looked at at all: to tell when it is no longer as it was.
    last: std::sync::Mutex<Option<(Arc<UsedUp>, Vec<String>)>>,
}

impl Desk {
    /// Success belongs to the exact credential that answered, for the auth cache's minute.
    pub fn login_verified(&self, tool: &str, home: &Path, now: i64) -> bool {
        let file = clarp_backend::by_id(tool).and_then(|b| b.login_file(home));
        let stamp = file.and_then(|f| crate::logins::digest(&f));
        self.verified
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .get(tool)
            .is_some_and(|(seen, at)| Some(*seen) == stamp && now - *at < 60_000)
    }

    pub async fn refresh(&self, app: &crate::edge::App, home: &Path, force: bool) -> Vec<Value> {
        let _one = self.refresh.lock().await;
        let provider = crate::usage_provider::Provider {
            loopback: app.config.usage_loopback,
        };
        let mut events = Vec::new();
        for name in [CLAUDE, CODEX, "agy"] {
            let with = With::of(app, home);
            let mut known = self.known.lock().await;
            let instance = format!("{}:{name}", with.server_id);
            let tool = Tool {
                provider: name,
                instance: &instance,
            };
            let stamp = || {
                clarp_backend::by_id(name)?
                    .login_file(home)
                    .and_then(|file| crate::logins::digest(&file))
            };
            let before = stamp();
            let account = if name == CLAUDE {
                known.claude_account(with, true).await
            } else {
                None
            };
            let login = if name == CODEX {
                known.login(with, name, true).await
            } else {
                None
            };
            let due = match name {
                CLAUDE => {
                    let Some(kept) = known.standing(with.fleet).await else {
                        continue;
                    };
                    kept.refresh_due(
                        account.as_deref(),
                        self.last_failure(with, CLAUDE).await,
                        with.now_ms,
                    )
                }
                CODEX => {
                    let Some(kept) = known.of(with.fleet, name).await else {
                        continue;
                    };
                    kept.refresh_due(login.as_ref().map(Named::login), with.now_ms)
                }
                _ => {
                    if !crate::logins::installed(&clarp_backend::program("agy").unwrap_or_default())
                    {
                        continue;
                    }
                    let last = self.agy_read(with).await;
                    let attempt = *self
                        .agy_attempt
                        .lock()
                        .unwrap_or_else(PoisonError::into_inner);
                    clarp_kernel::usage_ask::due(attempt, with.now_ms)
                        && clarp_kernel::usage_ask::due(last["at"].as_i64(), with.now_ms)
                }
            };
            drop(known);
            if !force && !due {
                continue;
            }
            if name == "agy" {
                *self
                    .agy_attempt
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner) = Some(with.now_ms);
            }
            let Ok(_login) = crate::pool::Service::use_login(app, name).await else {
                continue;
            };
            let answer = provider.ask(name, home, with.now_ms, before).await;
            let with = With::of(app, home);
            let mut known = self.known.lock().await;
            if before != stamp()
                || (name == CODEX
                    && known
                        .login(with, CODEX, false)
                        .await
                        .as_ref()
                        .map(Named::login)
                        != login.as_ref().map(Named::login))
            {
                continue;
            }
            if name == "agy" {
                if let Some(record) = answer {
                    let data = json!({"at":with.now_ms,"groups":record.value});
                    let _ = with
                        .fleet
                        .keep(KIND, name.into(), Some(data.to_string().into_bytes()))
                        .await;
                }
                continue;
            }
            let mut verified = false;
            match name {
                CLAUDE => {
                    if known.claude_account(with, false).await != account {
                        continue;
                    }
                    let Some(mut kept) = known.standing(with.fleet).await else {
                        continue;
                    };
                    if let Some(record) = answer {
                        if kept.seen_at_ms().is_none_or(|at| at <= with.now_ms) {
                            verified = kept.seen(
                                &record.value,
                                account.as_deref(),
                                record.source,
                                with.now_ms,
                            );
                            known.keep_standing(with.fleet, kept.clone()).await;
                        }
                    } else if kept.seen_at_ms().is_none() {
                        let _ = with
                            .fleet
                            .keep(
                                KIND,
                                "claude-failure".into(),
                                Some(with.now_ms.to_string().into_bytes()),
                            )
                            .await;
                    }
                    self.looked(
                        with,
                        name,
                        kept.used_up(tool, account.as_deref(), with.now_ms),
                    );
                }
                _ => {
                    let Some(mut kept) = known.of(with.fleet, name).await else {
                        continue;
                    };
                    let on = login.as_ref().map(Named::login);
                    if let (Some(record), Some(on)) = (answer, on) {
                        for event in kept.seen(
                            tool,
                            on,
                            &record.value,
                            record.source,
                            with.now_ms,
                            &mut drawn,
                        ) {
                            with.hub.publish(event.clone(), with.now_ms);
                            events.push(event);
                        }
                        verified = true;
                    } else {
                        kept.refresh_failed(with.now_ms);
                    }
                    known.keep(with.fleet, name, kept.clone()).await;
                    self.looked(with, name, kept.used_up(tool, on, with.now_ms));
                }
            }
            let mut cache = self.verified.lock().unwrap_or_else(PoisonError::into_inner);
            cache.remove(name);
            if let Some(stamp) = before.filter(|_| verified) {
                cache.insert(name.into(), (stamp, with.now_ms));
            }
        }
        events
    }

    async fn last_failure(&self, with: With<'_>, name: &str) -> Option<i64> {
        let bytes = with
            .fleet
            .kept(KIND, format!("{name}-failure"))
            .await
            .ok()??;
        std::str::from_utf8(&bytes).ok()?.parse().ok()
    }

    async fn agy_read(&self, with: With<'_>) -> Value {
        with.fleet
            .kept(KIND, "agy".into())
            .await
            .ok()
            .flatten()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
            .unwrap_or(Value::Null)
    }

    pub async fn asked_agy(&self, with: With<'_>) -> Value {
        let kept = self.agy_read(with).await;
        clarp_kernel::usage_ask::agy_provider(
            &format!("{}:agy", with.server_id),
            &kept["groups"],
            kept["at"].as_i64().unwrap_or(0),
            with.now_ms,
        )
    }

    /// A turn of a tool ended, in the tool's own session: what the tool wrote of its
    /// account's limits is read, kept, and what follows from it told, and so is what
    /// follows from how the turn ended. A tool that writes no such record, and a turn that
    /// wrote none, leave the windows as they are.
    pub async fn turn_ended(
        &self,
        with: With<'_>,
        tool: &str,
        session: &str,
        how: Ending,
        said: Option<LimitsSaid>,
    ) {
        if tool == CLAUDE {
            return self.claude_turn_ended(with, how, said).await;
        }
        if tool != CODEX {
            return;
        }
        let read = written_in(with.home, session).await;
        let mut known = self.known.lock().await;
        let Some(before) = known.of(with.fleet, tool).await else {
            return;
        };
        let mut kept = before.clone();
        // The key that logins are named with is drawn with the first thing there is to keep.
        let to_keep = read.is_some() || how == Ending::Refused;
        let on = known.login(with, tool, to_keep).await;
        let login = on.as_ref().map(Named::login);
        let instance = format!("{}:{tool}", with.server_id);
        let of = Tool {
            instance: &instance,
            provider: tool,
        };
        let mut events = Vec::new();
        if let (Some((snapshot, written_ms)), Some(on)) = (&read, &on)
            && let Some(at_ms) = taken(*written_ms, on.since_ms, kept.seen_at_ms(), with.now_ms)
        {
            events = kept.seen(of, on.login(), snapshot, SEEN_IN, at_ms, &mut drawn);
        }
        match how {
            // An account that served a turn to its end was not used up when it did, and a
            // limit of which nothing more was known is over.
            Ending::Completed(at_ms) => {
                let at_ms = at_ms.min(with.now_ms);
                events.extend(kept.completed(of, login, at_ms, &mut drawn));
            }
            // A turn that failed for a usage limit is a limit, whatever else is known of it.
            Ending::Refused => events.extend(kept.refused(of, login, with.now_ms, &mut drawn)),
            Ending::Otherwise => {}
        }
        if kept != before {
            known.keep(with.fleet, tool, kept.clone()).await;
        }
        for event in events {
            with.hub.publish(event, with.now_ms);
        }
        self.looked(with, tool, kept.used_up(of, login, with.now_ms));
    }

    /// A turn of Claude Code ended: the newest it said of its account's windows in the turn
    /// is kept as the word there is of that account, in the place of the one before, and a
    /// turn that completed is remembered as one the account served.
    async fn claude_turn_ended(&self, with: With<'_>, how: Ending, said: Option<LimitsSaid>) {
        let word = said.map(|said| (clarp_backend::limits::claude(&said.said), said.at_ms));
        let any = |windows: &Value| windows.as_object().is_some_and(|said| !said.is_empty());
        let word = word.filter(|(windows, _)| any(windows));
        let mut known = self.known.lock().await;
        let Some(before) = known.standing(with.fleet).await else {
            return;
        };
        let mut kept = before.clone();
        // The key that accounts are named with is drawn with the first word there is.
        let account = known.claude_account(with, word.is_some()).await;
        if let Some((windows, at_ms)) = &word {
            let at_ms = (*at_ms).min(with.now_ms);
            // Kept once: a Host that starts again tells the last turn of every agent once
            // more, and of two agents' words the newer stands.
            if kept.seen_at_ms().is_none_or(|seen| at_ms > seen) {
                kept.seen(windows, account.as_deref(), SAID_BY_CLAUDE, at_ms);
            }
        }
        if let Ending::Completed(at_ms) = how {
            kept.completed(at_ms.min(with.now_ms));
        }
        if kept != before {
            known.keep_standing(with.fleet, kept.clone()).await;
        }
        let instance = format!("{}:{CLAUDE}", with.server_id);
        let of = Tool {
            instance: &instance,
            provider: CLAUDE,
        };
        self.looked(
            with,
            CLAUDE,
            kept.used_up(of, account.as_deref(), with.now_ms),
        );
    }

    /// What an app's usage screen is shown of Claude, on the account Claude Code is on now.
    pub async fn asked_claude(&self, with: With<'_>) -> Value {
        let mut known = self.known.lock().await;
        let account = known.claude_account(with, false).await;
        let kept = known.standing(with.fleet).await.unwrap_or_default();
        let instance = format!("{}:{CLAUDE}", with.server_id);
        let of = Tool {
            instance: &instance,
            provider: CLAUDE,
        };
        self.looked(
            with,
            CLAUDE,
            kept.used_up(of, account.as_deref(), with.now_ms),
        );
        kept.provider(of, account.as_deref(), with.now_ms)
    }

    /// What an app's usage screen is shown of a tool, on the login the tool is on now. A
    /// limit whose window began anew since it was told is told as no longer known, to this
    /// app in the answer and to the others as it is told anything.
    pub async fn asked(&self, with: With<'_>, tool: &str) -> Shown {
        let mut known = self.known.lock().await;
        let on = known.login(with, tool, false).await;
        let before = known.of(with.fleet, tool).await;
        let mut kept = before.clone().unwrap_or_default();
        let instance = format!("{}:{tool}", with.server_id);
        let of = Tool {
            instance: &instance,
            provider: tool,
        };
        let login = on.as_ref().map(Named::login);
        let shown = kept.asked(of, login, with.now_ms, &mut drawn);
        // Written back where it changed, and never over what could not be read.
        if before.is_some_and(|before| before != kept) {
            known.keep(with.fleet, tool, kept.clone()).await;
        }
        for event in &shown.events {
            with.hub.publish(event.clone(), with.now_ms);
        }
        self.looked(with, tool, kept.used_up(of, login, with.now_ms));
        shown
    }

    /// Which accounts are used up now: looked at afresh, tool by tool.
    pub async fn used_up(&self, with: With<'_>) -> Arc<UsedUp> {
        let mut known = self.known.lock().await;
        let on = known.login(with, CODEX, false).await;
        let kept = known.of(with.fleet, CODEX).await.unwrap_or_default();
        let instance = format!("{}:{CODEX}", with.server_id);
        let of = Tool {
            instance: &instance,
            provider: CODEX,
        };
        let login = on.as_ref().map(Named::login);
        self.looked(with, CODEX, kept.used_up(of, login, with.now_ms));
        let account = known.claude_account(with, false).await;
        let kept = known.standing(with.fleet).await.unwrap_or_default();
        let instance = format!("{}:{CLAUDE}", with.server_id);
        let of = Tool {
            instance: &instance,
            provider: CLAUDE,
        };
        self.looked(
            with,
            CLAUDE,
            kept.used_up(of, account.as_deref(), with.now_ms),
        )
    }

    /// Remembers whether a tool's account is used up now (`now`: what an agent's row says
    /// of it then). Where an app would show another thing of it than at the last look (which
    /// window, until when), the apps are told to read their agents' rows again. The first
    /// look at a tool tells nothing: nothing is known to have changed.
    fn looked(&self, with: With<'_>, tool: &str, now: Option<Value>) -> Arc<UsedUp> {
        let shown = |all: &UsedUp| {
            let said = all.get(tool)?;
            Some((said["window"].clone(), said["resets_at"].clone()))
        };
        let mut last = self.last.lock().unwrap_or_else(PoisonError::into_inner);
        let (before, looked_at) = last.get_or_insert_with(Default::default);
        let mut all: UsedUp = before.as_ref().clone();
        match now {
            Some(said) => all.insert(tool.to_string(), said),
            None => all.remove(tool),
        };
        let first_look = !looked_at.iter().any(|at| at == tool);
        if first_look {
            looked_at.push(tool.to_string());
        }
        let changed = !first_look && shown(before) != shown(&all);
        let all = Arc::new(all);
        *before = all.clone();
        drop(last);
        if changed {
            let again = json!({ "type": "agent-roster", "kind": "backend-quota" });
            with.hub.publish(again, with.now_ms);
        }
        all
    }
}

/// Which accounts are used up now, for an app's request.
pub async fn used_up(app: &crate::edge::App) -> Arc<UsedUp> {
    let home = crate::config::home();
    app.limits.used_up(With::of(app, &home)).await
}

/// That an account is used up stops being so with time, and nothing happens when it does:
/// what was said of it grows old, its window begins anew. So it is looked at now and then
/// (every five minutes, as the Python Host looks), and an app that shows an account as used
/// up is told to look again.
pub async fn watch(
    desk: Arc<Desk>,
    fleet: FleetHandle,
    hub: Arc<Hub>,
    clock: Arc<dyn Clock + Send + Sync>,
    server_id: String,
) {
    let home = crate::config::home();
    loop {
        tokio::time::sleep(LOOKED_EVERY).await;
        let with = With {
            fleet: &fleet,
            hub: &hub,
            server_id: &server_id,
            home: &home,
            now_ms: clock.now_ms(),
        };
        desk.used_up(with).await;
    }
}

/// What the desk has in hand.
#[derive(Default)]
struct Known {
    /// What is kept of each tool's account, by the tool's name, once it was read.
    kept: BTreeMap<String, Kept>,
    /// The key that logins are named with, once it was read or drawn.
    key: Option<Vec<u8>>,
    /// The word there is of the Claude account, once it was read.
    standing: Option<Standing>,
}

impl Known {
    /// What is kept of a tool's account: read from the fleet the first time. `None` where
    /// the fleet cannot be asked: nothing is known then, and nothing may be written over.
    async fn of(&mut self, fleet: &FleetHandle, tool: &str) -> Option<Kept> {
        if let Some(kept) = self.kept.get(tool) {
            return Some(kept.clone());
        }
        let written = fleet.kept(KIND, tool.to_string()).await.ok()?;
        let read = written.and_then(|bytes| serde_json::from_slice::<Kept>(&bytes).ok());
        let kept = read.unwrap_or_default();
        self.kept.insert(tool.to_string(), kept.clone());
        Some(kept)
    }

    async fn keep(&mut self, fleet: &FleetHandle, tool: &str, kept: Kept) {
        if let Ok(written) = serde_json::to_vec(&kept) {
            let _ = fleet.keep(KIND, tool.to_string(), Some(written)).await;
        }
        self.kept.insert(tool.to_string(), kept);
    }

    /// The word there is of the Claude account: read from the fleet the first time. `None`
    /// where the fleet cannot be asked.
    async fn standing(&mut self, fleet: &FleetHandle) -> Option<Standing> {
        if let Some(kept) = &self.standing {
            return Some(kept.clone());
        }
        let written = fleet.kept(KIND, CLAUDE.to_string()).await.ok()?;
        let read = written.and_then(|bytes| serde_json::from_slice::<Standing>(&bytes).ok());
        let kept = read.unwrap_or_default();
        self.standing = Some(kept.clone());
        Some(kept)
    }

    async fn keep_standing(&mut self, fleet: &FleetHandle, kept: Standing) {
        if let Ok(written) = serde_json::to_vec(&kept) {
            let _ = fleet.keep(KIND, CLAUDE.to_string(), Some(written)).await;
        }
        self.standing = Some(kept);
    }

    /// The account Claude Code is on, named with this Host's key. `None`: it is on no login
    /// that holds (none, or one that ran out), nothing in its files says whose it is, or
    /// there is no key yet and none is to be drawn.
    async fn claude_account(&mut self, with: With<'_>, draw: bool) -> Option<String> {
        let file = clarp_backend::by_id(CLAUDE)?.login_file(with.home)?;
        let key = self.key(with.fleet, draw).await?;
        let now_ms = with.now_ms;
        let read = tokio::task::spawn_blocking(move || {
            // A login that is there and has not run out, as the Python Host asks for one.
            let (there, expires_ms) = crate::logins::credential(CLAUDE, &file);
            if !there || (expires_ms != 0 && expires_ms <= now_ms) {
                return None;
            }
            claude_marker(file.parent()?)
        });
        let marker = read.await.ok().flatten()?;
        let named = format!("claude-account\0{marker}");
        Some(name_of(&key, "claude-account", &named))
    }

    /// The key that logins are named with: read from the fleet the first time, and drawn
    /// where there is none and one is wanted (`draw`).
    async fn key(&mut self, fleet: &FleetHandle, draw: bool) -> Option<Vec<u8>> {
        if self.key.is_none() {
            self.key = match fleet.kept(KIND, KEY.to_string()).await.ok()? {
                None if draw => {
                    let key = crate::ids::hex(32).into_bytes();
                    let kept = fleet.keep(KIND, KEY.to_string(), Some(key.clone()));
                    kept.await.ok()?;
                    Some(key)
                }
                read => read,
            };
        }
        self.key.clone()
    }

    /// The login a tool is on, named with this Host's key. `None`: the tool is on no login
    /// that has an account's windows (no file, a key of the provider's in place of a login,
    /// no account in it), or there is no key yet and none is to be drawn (nothing was ever
    /// kept then, nothing is shown, and no login is read).
    async fn login(&mut self, with: With<'_>, tool: &str, draw: bool) -> Option<Named> {
        let file = clarp_backend::by_id(tool)?.login_file(with.home)?;
        let key = self.key(with.fleet, draw).await?;
        let read = tokio::task::spawn_blocking(move || {
            let written = std::fs::metadata(&file).and_then(|of| of.modified()).ok()?;
            let since = written.duration_since(UNIX_EPOCH).ok()?;
            Some((std::fs::read(&file).ok()?, since.as_millis() as i64))
        });
        let (bytes, since_ms) = read.await.ok().flatten()?;
        named(&bytes, &key, since_ms)
    }
}

/// An id nobody has, after its prefix.
fn drawn(prefix: &str) -> String {
    format!("{prefix}{}", crate::ids::uuid())
}

/// What Codex wrote of its account's limits in the newest record of a session's rollout, as
/// its reader reads it, and when it was written.
async fn written_in(home: &Path, session: &str) -> Option<(Value, i64)> {
    if session.is_empty() {
        return None;
    }
    let sessions = crate::past::codex_home(home).join("sessions");
    let named = session.to_string();
    let read = tokio::task::spawn_blocking(move || {
        let rollout = clarp_backend::past::codex_rollout(&sessions, &named)?;
        newest_record(&rollout)
    });
    let (record, written_ms) = read.await.ok().flatten()?;
    let snapshot = clarp_backend::limits::codex(&json!({ "rate_limit": record })).ok()?;
    Some((snapshot, written_ms))
}

/// Whether a record is taken, and as seen when. It is taken as seen when it was written,
/// and no later than now, whatever the clocks say. Not taken: a record written before the
/// login file was (it is of the login before, which nobody can name now), and one that is
/// no newer than the newest one kept (a turn that wrote none of its own leaves the turn
/// before's as the newest of its rollout, and a Host that starts again reads the last turn
/// of every agent once more).
fn taken(written_ms: i64, login_since_ms: i64, kept_ms: Option<i64>, now_ms: i64) -> Option<i64> {
    let at_ms = written_ms.min(now_ms);
    let of_this_login = written_ms >= login_since_ms;
    let newer = kept_ms.is_none_or(|kept| at_ms > kept);
    (of_this_login && newer).then_some(at_ms)
}

/// The newest record of limits in a rollout, and when it was written, in milliseconds: read
/// from the end of the file.
fn newest_record(rollout: &Path) -> Option<(Value, i64)> {
    let mut file = std::fs::File::open(rollout).ok()?;
    let size = file.metadata().ok()?.len();
    file.seek(SeekFrom::Start(size.saturating_sub(TAIL))).ok()?;
    let mut tail = Vec::new();
    file.take(TAIL).read_to_end(&mut tail).ok()?;
    // Where the tail begins inside a line, the rest of that line is no line: it is not one
    // of the records, and is passed over as every line that is none.
    let text = String::from_utf8_lossy(&tail);
    text.lines().rev().find_map(record_of)
}

/// The record of limits in a line of a rollout, where the line is a count of tokens that
/// has one, and when the line was written.
fn record_of(line: &str) -> Option<(Value, i64)> {
    if !line.contains("\"rate_limits\"") {
        return None;
    }
    let row: Value = serde_json::from_str(line).ok()?;
    let payload = &row["payload"];
    if row["type"] != "event_msg" || payload["type"] != "token_count" {
        return None;
    }
    let record = payload.get("rate_limits").filter(|said| said.is_object())?;
    let written_ms = quota::from_text(row["timestamp"].as_str()?)?;
    Some((record.clone(), written_ms))
}

/// A login, named: which login it is, whose account, and since when the file says so.
struct Named {
    generation: String,
    account: String,
    since_ms: i64,
}

impl Named {
    fn login(&self) -> Login<'_> {
        Login {
            generation: &self.generation,
            account: Some(&self.account),
        }
    }
}

/// A login file's login, named with a key: as the Python Host names one
/// (`backend_usage_store._codex_identity`), with this Host's own key. The login of a
/// ChatGPT account only: a key of the provider's has no such windows.
fn named(login_file: &[u8], key: &[u8], since_ms: i64) -> Option<Named> {
    let read: Value = serde_json::from_slice(login_file).ok()?;
    if read["auth_mode"] != "chatgpt" {
        return None;
    }
    let said = |name: &str| {
        let text = read["tokens"][name].as_str().unwrap_or("").trim();
        (!text.is_empty()).then_some(text)
    };
    let (token, account) = (said("access_token")?, said("account_id")?);
    Some(Named {
        generation: name_of(key, "auth", &format!("auth\0{account}\0{token}")),
        account: name_of(key, "account", &format!("account\0{account}")),
        since_ms,
    })
}

/// A name for something of a login that says nothing of it: its kind, and a keyed digest of
/// what it is.
fn name_of(key: &[u8], kind: &str, what: &str) -> String {
    let digest: String = keyed(key, what.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    format!("{kind}-{}", &digest[..24])
}

/// What tells one Claude account from another across renewals of its login, as the Python
/// Host finds it (`_claude_account_marker`): who Claude Code's own file says is logged in
/// (the account, its organization, the address, the last without its capitals); else the
/// profile that is in place, where profiles are kept; else what the login file itself says
/// of them. Never a token. `config` is Claude Code's own directory.
fn claude_marker(config: &Path) -> Option<String> {
    let read =
        |file: &Path| -> Option<Value> { serde_json::from_slice(&std::fs::read(file).ok()?).ok() };
    let who = |said: &Value, by: &str| -> Option<String> {
        let part = |name: &str| said[name].as_str().unwrap_or("").trim().to_string();
        let (account, organization) = (part("accountUuid"), part("organizationUuid"));
        let address = part("emailAddress").to_lowercase();
        let any = !(account.is_empty() && organization.is_empty() && address.is_empty());
        any.then(|| format!("{by}:{account}\0{organization}\0{address}"))
    };
    let named = std::env::var_os("CLAUDE_CONFIG_FILE").filter(|named| !named.is_empty());
    let mut files: Vec<std::path::PathBuf> = named.map(Into::into).into_iter().collect();
    files.push(config.join(".claude.json"));
    files.extend(config.parent().map(|above| above.join(".claude.json")));
    let logged_in = files.iter().filter_map(|file| read(file));
    let by_its_file = logged_in
        .filter(|said| said["oauthAccount"].is_object())
        .find_map(|said| who(&said["oauthAccount"], "oauth"));
    let by_profile = || {
        let profile = std::fs::read_to_string(config.join("profiles/.current_profile")).ok()?;
        let profile = profile.trim();
        (!profile.is_empty()).then(|| format!("profile:{profile}"))
    };
    let by_login = || {
        who(
            &read(&config.join(".credentials.json"))?["claudeAiOauth"],
            "cred",
        )
    };
    by_its_file.or_else(by_profile).or_else(by_login)
}

/// HMAC-SHA-256 (RFC 2104) of a message under a key.
fn keyed(key: &[u8], message: &[u8]) -> [u8; 32] {
    let mut block = [0u8; 64];
    if key.len() > block.len() {
        block[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let pad = |with: u8| block.map(|byte| byte ^ with);
    let inner = Sha256::new()
        .chain_update(pad(0x36))
        .chain_update(message)
        .finalize();
    let outer = Sha256::new().chain_update(pad(0x5c)).chain_update(inner);
    outer.finalize().into()
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|byte| format!("{byte:02x}")).collect()
    }

    #[test]
    fn the_keyed_digest_is_hmac_sha_256() {
        // The second case of RFC 4231, and a key longer than a block (Python's `hmac` says
        // the same of both).
        assert_eq!(
            hex(&keyed(b"Jefe", b"what do ya want for nothing?")),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
        );
        assert_eq!(
            hex(&keyed(&[b'k'; 100], b"longer than a block")),
            "0778aaa1a01766e6369f9e44945a2b111ae8cdf21c31421d517915da7bb0eab3"
        );
    }

    #[test]
    fn a_login_is_named_without_what_is_in_it() {
        let file = |account: &str, token: &str| {
            json!({ "auth_mode": "chatgpt", "tokens": { "account_id": account, "access_token": token } })
                .to_string()
                .into_bytes()
        };
        let one = named(&file("an-account", "a-token"), b"a key", 7).unwrap();
        // Neither the account nor the token is in the names, and the names are the same
        // whenever they are made of the same login with the same key.
        for name in [&one.generation, &one.account] {
            assert!(
                !name.contains("an-account") && !name.contains("a-token"),
                "{name}"
            );
        }
        assert_eq!(one.generation.len(), "auth-".len() + 24);
        assert_eq!(one.account.len(), "account-".len() + 24);
        assert_eq!(one.since_ms, 7);
        let again = named(&file("an-account", "a-token"), b"a key", 7).unwrap();
        assert_eq!(
            (&again.generation, &again.account),
            (&one.generation, &one.account)
        );
        // The login renewed: another login, the same account.
        let renewed = named(&file("an-account", "another-token"), b"a key", 7).unwrap();
        assert_ne!(renewed.generation, one.generation);
        assert_eq!(renewed.account, one.account);
        // Another account: another of both. Another key: another of both.
        let other = named(&file("another-account", "a-token"), b"a key", 7).unwrap();
        assert!(other.generation != one.generation && other.account != one.account);
        let elsewhere = named(&file("an-account", "a-token"), b"another key", 7).unwrap();
        assert!(elsewhere.generation != one.generation && elsewhere.account != one.account);
        // What is no login of an account.
        let key_of_the_provider = json!({ "auth_mode": "apikey", "OPENAI_API_KEY": "a-key" });
        let no_account = json!({ "auth_mode": "chatgpt", "tokens": { "access_token": "a-token" } });
        let no_token = json!({ "auth_mode": "chatgpt", "tokens": { "account_id": "an-account" } });
        for none in [
            key_of_the_provider,
            no_account,
            no_token,
            json!({}),
            json!("a text"),
        ] {
            assert!(
                named(none.to_string().as_bytes(), b"a key", 7).is_none(),
                "{none}"
            );
        }
        assert!(named(b"not json", b"a key", 7).is_none());
    }

    #[test]
    fn the_newest_record_of_a_rollout_is_read_from_its_end() {
        let dir = std::env::temp_dir().join(format!("clarp-quota-{}", crate::ids::hex(6)));
        std::fs::create_dir_all(&dir).unwrap();
        let rollout = dir.join("rollout.jsonl");
        let count = |at: &str, used: f64| {
            json!({
                "timestamp": at, "ordinal": 1, "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": { "total_token_usage": { "input_tokens": 5 } },
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": { "used_percent": used, "window_minutes": 10_080, "resets_at": 1_900_400_000 },
                        "secondary": null,
                    },
                },
            })
            .to_string()
        };
        // Lines that are no record: another event, a count of tokens without a record, a
        // record in an event that is no count of tokens, a line that is no event at all.
        let other = json!({ "timestamp": "2030-03-17T17:46:42.000Z", "type": "response_item", "payload": { "type": "message" } }).to_string();
        let bare = json!({ "timestamp": "2030-03-17T17:46:43.000Z", "type": "event_msg", "payload": { "type": "token_count", "info": null, "rate_limits": null } }).to_string();
        let elsewhere = json!({ "timestamp": "2030-03-17T17:46:44.000Z", "type": "event_msg", "payload": { "type": "other", "rate_limits": { "primary": {} } } }).to_string();
        let lines = [
            count("2030-03-17T17:46:40.000Z", 10.0),
            count("2030-03-17T17:46:41.500Z", 12.5),
            other,
            bare,
            elsewhere,
            "\"rate_limits\" and no event".to_string(),
        ];
        std::fs::write(&rollout, lines.join("\n") + "\n").unwrap();
        let (record, written_ms) = newest_record(&rollout).unwrap();
        assert_eq!(record["primary"]["used_percent"], 12.5);
        assert_eq!(written_ms, 1_900_000_001_500);
        // A rollout longer than what is read of it: the newest record is found all the same,
        // and one that lies before what is read is not looked for.
        let filler = format!("{}\n", "x".repeat(1023)).repeat(1100);
        std::fs::write(&rollout, format!("{}\n{filler}{}\n", lines[0], lines[1])).unwrap();
        assert_eq!(newest_record(&rollout).unwrap().1, 1_900_000_001_500);
        std::fs::write(&rollout, format!("{}\n{filler}", lines[0])).unwrap();
        assert!(newest_record(&rollout).is_none());
        assert!(newest_record(&dir.join("none.jsonl")).is_none());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_record_is_taken_once_and_only_of_the_login_that_is_there() {
        // Written after the login was, nothing kept: taken, as seen when it was written.
        assert_eq!(taken(1_000, 900, None, 5_000), Some(1_000));
        // A clock ahead: no later than now.
        assert_eq!(taken(9_000, 900, None, 5_000), Some(5_000));
        // Written before the login file was: of the login before.
        assert_eq!(taken(1_000, 1_001, None, 5_000), None);
        assert_eq!(taken(1_000, 1_000, None, 5_000), Some(1_000));
        // No newer than the newest kept: read before.
        assert_eq!(taken(1_000, 900, Some(1_000), 5_000), None);
        assert_eq!(taken(1_000, 900, Some(2_000), 5_000), None);
        assert_eq!(taken(1_001, 900, Some(1_000), 5_000), Some(1_001));
    }

    #[test]
    fn a_claude_account_is_told_by_who_claude_code_says_is_logged_in() {
        let above = std::env::temp_dir().join(format!("clarp-claude-who-{}", crate::ids::hex(6)));
        let config = above.join("claude");
        std::fs::create_dir_all(config.join("profiles")).unwrap();
        let write = |file: &Path, said: Value| std::fs::write(file, said.to_string()).unwrap();
        // Nothing says who: no marker.
        assert_eq!(claude_marker(&config), None);
        // The login file itself says who, where nothing else does.
        let login = json!({ "accessToken": "a-token", "accountUuid": "acct-1" });
        write(
            &config.join(".credentials.json"),
            json!({ "claudeAiOauth": login }),
        );
        assert_eq!(claude_marker(&config).unwrap(), "cred:acct-1\0\0");
        // The profile that is in place counts before that.
        std::fs::write(config.join("profiles/.current_profile"), " work \n").unwrap();
        assert_eq!(claude_marker(&config).unwrap(), "profile:work");
        // Claude Code's own file counts before both: beside its directory, and in it first.
        let who = json!({ "accountUuid": " acct-2 ", "organizationUuid": "org-2", "emailAddress": "Someone@Example.Org" });
        write(&above.join(".claude.json"), json!({ "oauthAccount": who }));
        let beside = "oauth:acct-2\0org-2\0someone@example.org";
        assert_eq!(claude_marker(&config).unwrap(), beside);
        write(
            &config.join(".claude.json"),
            json!({ "oauthAccount": { "organizationUuid": "org-3" } }),
        );
        assert_eq!(claude_marker(&config).unwrap(), "oauth:\0org-3\0");
        // A file that names nobody is passed over for the next.
        write(&config.join(".claude.json"), json!({ "oauthAccount": {} }));
        assert_eq!(claude_marker(&config).unwrap(), beside);
        write(&config.join(".claude.json"), json!({ "numStartups": 4 }));
        assert_eq!(claude_marker(&config).unwrap(), beside);
        std::fs::write(config.join(".claude.json"), "not json").unwrap();
        assert_eq!(claude_marker(&config).unwrap(), beside);
        // The name made of a marker says nothing of it, and is the same for the same.
        let named = name_of(
            b"a key",
            "claude-account",
            &format!("claude-account\0{beside}"),
        );
        assert_eq!(named.len(), "claude-account-".len() + 24);
        assert!(
            !named.contains("acct") && !named.contains("someone"),
            "{named}"
        );
        assert_eq!(
            named,
            name_of(
                b"a key",
                "claude-account",
                &format!("claude-account\0{beside}")
            )
        );
        assert_ne!(
            named,
            name_of(
                b"another key",
                "claude-account",
                &format!("claude-account\0{beside}")
            )
        );
        std::fs::remove_dir_all(&above).unwrap();
    }
}
