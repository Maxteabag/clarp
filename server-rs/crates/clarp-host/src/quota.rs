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
//! advice for whoever is about to write to the agent, and no refusal. Before that, it says
//! that the account is near a limit (`state: near`, `clarp_kernel::quota_near`). Either is
//! said only where no other seat of the tool's pool would take the agent's turn ([`Rows`]).
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
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::fleet::FleetHandle;
use crate::hub::Hub;

/// What this desk keeps, declared once (`kept.rs`: the registry walks it at every start).
pub(crate) const KEPT: &[crate::kept::Kind] =
    &[crate::kept::Kind::book(KIND, crate::kept::Key::Plain, fits)];

/// Whether bytes are one of this desk's records, as it reads them. One table holds five shapes,
/// told apart by their keys, which a judge does not see: a tool's standing (`Kept`), Claude's
/// (`Standing`), and the rest as text (a failure's count, Antigravity's view, the key it draws,
/// which is no JSON). So no record that is text is set aside.
fn fits(kept: &[u8]) -> bool {
    crate::kept::fits::<Kept>(kept)
        || crate::kept::fits::<Standing>(kept)
        || crate::kept::text(kept)
}

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
/// How often at most the seats are read for a tool whose reading is old, where its rows ask
/// whether another seat would take a turn.
const READ_ASKED_EVERY_MS: i64 = 60_000;
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

// A broken actor's activity is unknown, so leave automatic refresh eligible.
fn agy_in_use(directory: &crate::fleet::Directory, now: i64) -> bool {
    directory
        .members
        .iter()
        .chain(&directory.janitors)
        // Antigravity over ACP is the same program and account.
        .filter(|member| matches!(member.record.backend.as_str(), "agy" | "agy-acp"))
        .any(|member| {
            let view = member.agent.view();
            view.broken.is_some()
                || clarp_kernel::usage_ask::agy_recent_activity(view.latest_turn_activity, now)
        })
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
    verified: std::sync::Mutex<BTreeMap<String, Verification>>,
    agy_attempt: std::sync::Mutex<Option<i64>>,
    /// Which accounts were used up and which near a limit when that was last looked at, and
    /// which tools were looked at at all: to tell when it is no longer as it was.
    last: std::sync::Mutex<Option<(Looked, Vec<String>)>>,
    /// When the first window that an agent's row names begins anew: it is looked at again
    /// then, so that what it said clears as it does. The watch is woken where it moves sooner.
    wake: std::sync::Mutex<Option<i64>>,
    /// When the first seat of a pool that a used-up row waits on regains room ([`look_again`]):
    /// looked at again then too, apart from the windows the tools named, which do not move it.
    pool_wake: std::sync::Mutex<Option<i64>>,
    /// The moment a seat regained room that the seats were last read again for: once for each,
    /// since a reading that still does not show it would otherwise ask for another at once.
    read_for: std::sync::Mutex<Option<i64>>,
    /// When the seats were last read because a tool's reading was old where a row asked whether
    /// another seat would take its turn ([`ask_reading`]), and how many such reads in a row left
    /// it old: one read for every tool, once a minute at most, and less often while they fail.
    read_asked: std::sync::Mutex<(Option<i64>, u32)>,
    woken: tokio::sync::Notify,
    /// Whether another seat would take the turns of a tool that is near a limit or used up, by
    /// the tool and the model an agent runs on it, as the apps were last told it
    /// ([`look_again`]): the word, and for a used-up account when the pool regains room.
    failover: std::sync::Mutex<Told>,
    /// Whether another seat of each tool's pool would take a turn on the tool's default model
    /// from an account that is used up, as last judged ([`look_again`]): what a hard limit of
    /// the tool is told with ([`Desk::pooled`]).
    pooled: std::sync::Mutex<BTreeMap<String, Pooled>>,
    /// A hard limit of each tool that was not told because the pool took the turn: told once the
    /// pool can no longer take it ([`judge_pools`]), as the tool tells no limit twice; let go
    /// when its limit is over.
    held: std::sync::Mutex<BTreeMap<String, Held>>,
}

/// Whether another seat of a tool's pool would take a turn from its used-up account, the
/// soonest moment a seat regains room where none would, and until when that holds: until the
/// reading of the seats it was judged by grows old, and five minutes at most, after which it
/// is judged again ([`watch`]).
#[derive(Debug, Clone)]
struct Pooled {
    /// From a used-up account (`quota_near::used_up_failover`): what a hard limit is told by.
    failover: clarp_kernel::quota_near::Failover,
    /// From an account near a limit (`quota_near::failover`, the rule of the near rows): what a
    /// warning is told by.
    near: clarp_kernel::quota_near::Failover,
    regains: Option<i64>,
    until_ms: i64,
    /// The seat the tool was on when it was judged (`quota_near::seat_in_use`): a hard limit
    /// held then is of that seat.
    seat: Option<String>,
}

/// A hard limit of a tool that was not told because the pool would take the turn, and the seat
/// it is of: the one the tool was on when it was held.
#[derive(Debug, Clone)]
struct Held {
    event: Value,
    seat: Option<String>,
}

/// What the apps were told of whether another seat would take the turns of a tool, by the tool
/// and the model an agent runs on it: the word, and for a used-up account the minute the pool
/// regains room.
type Told = BTreeMap<(String, String), (&'static str, Option<i64>)>;

/// Which accounts are used up, and which are near a limit and not used up, by the tool's
/// name, with what an agent's row says of its tool then.
#[derive(Clone, Default)]
pub struct Looked {
    pub used_up: Arc<UsedUp>,
    pub near: Arc<UsedUp>,
    /// What is known of the windows of the account each tool is on now, as an agent's row
    /// says it (`account_quota`, issue 869): only where something is known of them.
    pub accounts: Arc<UsedUp>,
}

/// What a tool's account is as an agent's row says it: used up, and else near a limit. Both
/// have one shape (state, provider_id, reason, window, resets_at, observed_at) and say how
/// much of the window they name is used (`used_percentage`, 0 to 100): on a used-up account
/// null where that is not known (a limit that a turn failed for).
type Said = (Option<Value>, Option<Value>, Option<Value>);

/// What an agent's row says of the windows of the account its tool is on (`account_quota`),
/// from what an app's usage screen is shown of the tool (`provider`): the five hours and the
/// week, how much of each is used and when it begins anew (UTC, to the second), when that was
/// said and whether that is fresh. Nothing where no window of either is known.
fn account_of(provider: &Value) -> Option<Value> {
    let windows: Vec<Value> = provider["windows"]
        .as_array()?
        .iter()
        .filter(|window| matches!(window["kind"].as_str(), Some("five_hour" | "seven_day")))
        .map(|window| {
            let resets_at = window["resets_at"].as_str().and_then(quota::from_text);
            let used = window["used_percentage"].as_f64();
            json!({
                "kind": window["kind"],
                "used_percentage": used.map(|used| used.clamp(0.0, 100.0)),
                "resets_at": resets_at.map(quota::moment),
            })
        })
        .collect();
    if windows.is_empty() {
        return None;
    }
    Some(json!({
        "provider_id": provider["provider_id"],
        "observed_at": provider["observed_at"],
        "freshness": provider["freshness"],
        "windows": windows,
    }))
}

/// A window as the apps are told again on it: its kind, its whole percent, its reset's minute.
type WindowShown = (Value, Option<f64>, Option<i64>);

/// What the apps are told again on, of an account's windows: freshness, and of each window
/// its whole percent and its reset to the minute (Codex says a reset a second apart from
/// record to record), so a meter follows without an event at every turn.
fn account_shown(account: Option<&Value>) -> Option<(Value, Vec<WindowShown>)> {
    let account = account?;
    let windows = account["windows"].as_array().into_iter().flatten();
    let windows = windows.map(|window| {
        let resets = window["resets_at"].as_str().and_then(quota::from_text);
        (
            window["kind"].clone(),
            window["used_percentage"].as_f64().map(f64::floor),
            resets.map(|ms| (ms + 30_000).div_euclid(60_000)),
        )
    });
    Some((account["freshness"].clone(), windows.collect()))
}

/// What is used of the window that a used-up account's row names, set on the row (no more
/// than all of it), and when it begins anew written as the row's other moments are (UTC, to
/// the second), whatever form the tool gave it in.
fn with_used(mut used_up: Option<Value>, used: impl FnOnce(&str) -> Option<f64>) -> Option<Value> {
    if let Some(said) = &mut used_up {
        let window = said["window"].as_str().unwrap_or("").to_string();
        let used = used(&window).map(|used| used.clamp(0.0, 100.0));
        said["used_percentage"] = json!(used);
        let resets_at = said["resets_at"].as_str().and_then(quota::from_text);
        said["resets_at"] = json!(resets_at.map(quota::moment));
    }
    used_up
}

fn codex_said(kept: &Kept, tool: Tool<'_>, login: Option<Login<'_>>, now_ms: i64) -> Said {
    let used_up = kept.used_up(tool, login, now_ms);
    let used_up = with_used(used_up, |window| kept.used_of(login, now_ms, window));
    let near = match &used_up {
        None => kept.near(tool, login, now_ms),
        Some(_) => None,
    };
    let account = account_of(&kept.provider(tool, login, now_ms));
    (used_up, near, account)
}

fn claude_said(kept: &Standing, tool: Tool<'_>, active: Option<&str>, now_ms: i64) -> Said {
    let used_up = kept.used_up(tool, active, now_ms);
    let used_up = with_used(used_up, |window| kept.used_of(window));
    let near = match &used_up {
        None => kept.near(tool, active, now_ms),
        Some(_) => None,
    };
    let account = account_of(&kept.provider(tool, active, now_ms));
    (used_up, near, account)
}

const VERIFICATION_MS: i64 = 60_000;

#[derive(Clone)]
pub(crate) struct LoginAccount {
    pub account: String,
    pub method: &'static str,
}

#[derive(Clone)]
enum LoginProof {
    Failed,
    Verified,
    Described(LoginAccount),
}

struct Verification {
    stamp: [u8; 32],
    at: i64,
    proof: LoginProof,
}

impl Verification {
    fn is_current(&self, stamp: [u8; 32], now: i64) -> bool {
        self.stamp == stamp && (0..VERIFICATION_MS).contains(&(now - self.at))
    }
}

impl Desk {
    fn login_proof(&self, tool: &str, stamp: [u8; 32], now: i64) -> Option<(i64, LoginProof)> {
        self.verified
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .get(tool)
            .filter(|seen| seen.is_current(stamp, now))
            .map(|seen| (seen.at, seen.proof.clone()))
    }

    /// A verification is for one exact credential and can be shared with usage refreshes.
    pub(crate) fn login_result(&self, tool: &str, stamp: [u8; 32], now: i64) -> Option<bool> {
        self.login_proof(tool, stamp, now)
            .map(|(_, proof)| !matches!(proof, LoginProof::Failed))
    }

    fn keep_verification(&self, tool: &str, stamp: [u8; 32], at: i64, proof: LoginProof) {
        self.verified
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .insert(tool.into(), Verification { stamp, at, proof });
    }

    fn keep_usage_verification(&self, tool: &str, stamp: [u8; 32], at: i64, valid: bool) {
        let mut known = self.verified.lock().unwrap_or_else(PoisonError::into_inner);
        if valid
            && known.get(tool).is_some_and(|seen| {
                seen.is_current(stamp, at) && matches!(seen.proof, LoginProof::Described(_))
            })
        {
            // Keep the account's original expiry. Another quota read must not extend it.
            return;
        }
        let proof = if valid {
            LoginProof::Verified
        } else {
            LoginProof::Failed
        };
        known.insert(tool.into(), Verification { stamp, at, proof });
    }

    pub(crate) async fn verify_login(
        &self,
        app: &crate::edge::App,
        tool: &'static str,
        home: &Path,
        stamp: [u8; 32],
        describe: impl Future<Output = LoginAccount>,
    ) -> Option<LoginAccount> {
        let cached = |now| self.login_proof(tool, stamp, now);
        match cached(app.clock.now_ms()) {
            Some((_, LoginProof::Described(account))) => return Some(account),
            Some((_, LoginProof::Failed)) => return None,
            _ => {}
        }
        let _one = self.refresh.lock().await;
        let now = app.clock.now_ms();
        let at = match cached(now) {
            Some((_, LoginProof::Described(account))) => return Some(account),
            Some((_, LoginProof::Failed)) => return None,
            Some((at, LoginProof::Verified)) => Some(at),
            None => None,
        };
        let _login = crate::pool::Service::use_login(app, tool).await.ok()?;
        let file = clarp_backend::by_id(tool)?.login_file(home)?;
        let current = || crate::logins::digest(&file) == Some(stamp);
        if !current() {
            return None;
        }
        let at = if let Some(at) = at {
            at
        } else {
            let provider = crate::usage_provider::Provider {
                loopback: app.config.usage_loopback,
            };
            let valid = provider.ask(tool, home, now, Some(stamp)).await.is_some();
            if !current() {
                return None;
            }
            let at = app.clock.now_ms();
            if !valid {
                self.keep_verification(tool, stamp, at, LoginProof::Failed);
                return None;
            }
            at
        };
        let account = describe.await;
        if !current() {
            return None;
        }
        self.keep_verification(tool, stamp, at, LoginProof::Described(account.clone()));
        Some(account)
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
                        && agy_in_use(&app.fleet.directory(), with.now_ms)
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
                    with.fleet
                        .keep_said(KIND, name.into(), Some(data.to_string().into_bytes()))
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
                        with.fleet
                            .keep_said(
                                KIND,
                                "claude-failure".into(),
                                Some(with.now_ms.to_string().into_bytes()),
                            )
                            .await;
                    }
                    self.looked(
                        with,
                        name,
                        claude_said(&kept, tool, account.as_deref(), with.now_ms),
                    );
                }
                _ => {
                    let Some(mut kept) = known.of(with.fleet, name).await else {
                        continue;
                    };
                    let on = login.as_ref().map(Named::login);
                    if let (Some(record), Some(on)) = (answer, on) {
                        let seen = kept.seen(
                            tool,
                            on,
                            &record.value,
                            record.source,
                            with.now_ms,
                            &mut drawn,
                        );
                        let told = seen
                            .into_iter()
                            .filter_map(|event| self.pooled(name, event, with.now_ms));
                        for event in told {
                            with.hub.publish(event.clone(), with.now_ms);
                            events.push(event);
                        }
                        verified = true;
                    } else {
                        kept.refresh_failed(with.now_ms);
                    }
                    known.keep(with.fleet, name, kept.clone()).await;
                    self.looked(with, name, codex_said(&kept, tool, on, with.now_ms));
                }
            }
            if let Some(stamp) = before {
                self.keep_usage_verification(name, stamp, with.now_ms, verified);
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
        // When the turn's own word of the windows was written, where it wrote a new one.
        let mut its_word_ms = None;
        if let (Some((snapshot, written_ms)), Some(on)) = (&read, &on)
            && let Some(at_ms) = taken(*written_ms, on.since_ms, kept.seen_at_ms(), with.now_ms)
        {
            events = kept.seen(of, on.login(), snapshot, SEEN_IN, at_ms, &mut drawn);
            its_word_ms = Some(at_ms);
        }
        match how {
            // An account that served a turn to its end was not used up when it did, and a
            // limit of which nothing more was known is over.
            Ending::Completed(at_ms) => {
                let at_ms = completed_at(at_ms, its_word_ms, with.now_ms);
                events.extend(kept.completed(of, login, at_ms, &mut drawn));
            }
            // A turn that failed for a usage limit is a limit, whatever else is known of it.
            Ending::Refused => events.extend(kept.refused(of, login, with.now_ms, &mut drawn)),
            Ending::Otherwise => {}
        }
        if kept != before {
            known.keep(with.fleet, tool, kept.clone()).await;
        }
        let told = events
            .into_iter()
            .filter_map(|event| self.pooled(tool, event, with.now_ms));
        for event in told {
            with.hub.publish(event, with.now_ms);
        }
        self.looked(with, tool, codex_said(&kept, of, login, with.now_ms));
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
            let its_word_ms = word.as_ref().map(|(_, said)| (*said).min(with.now_ms));
            kept.completed(completed_at(at_ms, its_word_ms, with.now_ms));
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
            claude_said(&kept, of, account.as_deref(), with.now_ms),
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
            claude_said(&kept, of, account.as_deref(), with.now_ms),
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
        self.looked(with, tool, codex_said(&kept, of, login, with.now_ms));
        shown
    }

    /// Which accounts are used up and which near a limit now: looked at afresh, tool by tool.
    pub async fn used_up(&self, with: With<'_>) -> Looked {
        let mut known = self.known.lock().await;
        let on = known.login(with, CODEX, false).await;
        let kept = known.of(with.fleet, CODEX).await.unwrap_or_default();
        let instance = format!("{}:{CODEX}", with.server_id);
        let of = Tool {
            instance: &instance,
            provider: CODEX,
        };
        let login = on.as_ref().map(Named::login);
        self.looked(with, CODEX, codex_said(&kept, of, login, with.now_ms));
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
            claude_said(&kept, of, account.as_deref(), with.now_ms),
        )
    }

    /// Remembers whether a tool's account is used up or near a limit now (`said`: what an
    /// agent's row says of it then). Where an app would show another thing of it than at the
    /// last look (used up or near, which window, until when, near by how many whole percent),
    /// the apps are told to read their agents' rows again. The first look at a tool tells
    /// nothing: nothing is known to have changed.
    fn looked(&self, with: With<'_>, tool: &str, (used_up, near, account): Said) -> Looked {
        let shown = |all: &Looked| {
            let (said, used) = match all.used_up.get(tool) {
                Some(said) => (said, Value::Null),
                None => {
                    let said = all.near.get(tool)?;
                    // In whole percents: the apps are told again as the share grows by one,
                    // and not at every turn.
                    (
                        said,
                        json!(said["used_percentage"].as_f64().map(f64::floor)),
                    )
                }
            };
            // To the minute: Codex says a window's reset a second apart from record to record.
            let resets = said["resets_at"].as_str().and_then(quota::from_text);
            let resets = resets.map(|ms| (ms + 30_000).div_euclid(60_000));
            let what = |field: &str| said[field].clone();
            Some((what("state"), what("window"), resets, used))
        };
        let mut last = self.last.lock().unwrap_or_else(PoisonError::into_inner);
        let (before, looked_at) = last.get_or_insert_with(Default::default);
        let keep = |of: &Arc<UsedUp>, said: Option<Value>| {
            let mut all: UsedUp = of.as_ref().clone();
            match said {
                Some(said) => all.insert(tool.to_string(), said),
                None => all.remove(tool),
            };
            Arc::new(all)
        };
        let all = Looked {
            used_up: keep(&before.used_up, used_up),
            near: keep(&before.near, near),
            accounts: keep(&before.accounts, account),
        };
        let first_look = !looked_at.iter().any(|at| at == tool);
        if first_look {
            looked_at.push(tool.to_string());
        }
        let changed = !first_look && shown(before) != shown(&all);
        let account_changed = !first_look
            && account_shown(before.accounts.get(tool)) != account_shown(all.accounts.get(tool));
        *before = all.clone();
        // The first window named that begins anew: looked at again when it has. Set while
        // what it is of is held, so that of two looks at once the later one's stands.
        let resets = all.used_up.values().chain(all.near.values());
        let wake = resets
            .filter_map(|said| said["resets_at"].as_str().and_then(quota::from_text))
            .filter(|at| *at > with.now_ms)
            .min();
        let mut kept = self.wake.lock().unwrap_or_else(PoisonError::into_inner);
        let sooner = wake.is_some_and(|wake| kept.is_none_or(|kept| wake < kept));
        *kept = wake;
        drop(kept);
        drop(last);
        if sooner {
            self.woken.notify_one();
        }
        if changed {
            let again = json!({ "type": "agent-roster", "kind": "backend-quota" });
            with.hub.publish(again, with.now_ms);
        }
        // The meter of the agents on the tool: a type of its own, which an app that does not
        // know it passes over, rather than a roster kind that would have it read the whole
        // snapshot as the share grows (issue 869).
        if account_changed {
            let told = json!({
                "type": "account-quota",
                "provider_id": tool,
                "account_quota": all.accounts.get(tool),
            });
            with.hub.publish(told, with.now_ms);
        }
        all
    }

    /// A `provider-limit` event of a tool, as the apps are told it: a hard limit not at all
    /// while another seat of the tool's pool would take the turn, else with `failover` and
    /// when the pool regains room (`clarp_kernel::quota_near::limit_event`). A judgement that
    /// no longer holds, or none yet, is not known: the limit is told, as `unknown`, since an
    /// old reading of the seats never hides one.
    fn pooled(&self, tool: &str, event: Value, now_ms: i64) -> Option<Value> {
        let pooled = self.pooled.lock().unwrap_or_else(PoisonError::into_inner);
        let holds = pooled.get(tool).filter(|pooled| now_ms < pooled.until_ms);
        let unknown = clarp_kernel::quota_near::Failover::Unknown;
        let (failover, near, regains) = holds.map_or((unknown, unknown, None), |pooled| {
            (pooled.failover, pooled.near, pooled.regains)
        });
        // The seat a limit is of is the one the tool is on, as last judged, also where that
        // judgement no longer holds: a switch judges again at once ([`look_again`]), so the last
        // seat judged is the seat in use. A limit held while the reading is old is then still of
        // a seat, and told once it is known that no seat would take the turn.
        let seat = pooled.get(tool).and_then(|pooled| pooled.seat.clone());
        drop(pooled);
        let mut held = self.held.lock().unwrap_or_else(PoisonError::into_inner);
        let kind = event["kind"].as_str().unwrap_or("").to_string();
        let warning = event["type"] == "provider-limit" && kind == "warning";
        let told = if warning {
            clarp_kernel::quota_near::warning_event(event.clone(), near)
        } else {
            clarp_kernel::quota_near::limit_event(event.clone(), failover, regains)
        };
        match (kind.as_str(), &told) {
            // Not told now: kept, to be told if the pool cannot take the turn after all.
            ("hard_limit", None) => {
                held.insert(tool.to_string(), Held { event, seat });
            }
            // A warning not told, as another seat would take the turn or that cannot be known:
            // kept the same way, unless a hard limit of the tool is held, which says more.
            ("warning", None) => {
                let hard = held
                    .get(tool)
                    .is_some_and(|held| held.event["kind"] == "hard_limit");
                if !hard {
                    held.insert(tool.to_string(), Held { event, seat });
                }
            }
            // Told, or over: nothing is held for it any more.
            ("hard_limit" | "recovered" | "unknown", _) => {
                held.remove(tool);
            }
            _ => {}
        }
        told
    }

    /// The hard limit held for a tool ([`Desk::pooled`]), to be told now that the pool cannot
    /// take the turn: with `failover` and when the pool regains room, and only while the tool is
    /// still on the seat the limit is of. A switch away from that seat took the turn, as the
    /// pool was to (judged after it, with the seat switched to taken as used up, no seat may be
    /// left, which says nothing of the limit held): the hold is let go. Nothing where none is
    /// held, or its window has begun anew since.
    fn release(&self, tool: &str, pooled: &Pooled, now_ms: i64) -> Option<Value> {
        let mut held = self.held.lock().unwrap_or_else(PoisonError::into_inner);
        let of = held.get_mut(tool)?;
        // A limit held before any seat was judged (none known when it came) is of the first seat
        // judged after: the tool was on it then too, as a switch is judged at once.
        if of.seat.is_none() {
            of.seat.clone_from(&pooled.seat);
        }
        // The tool left the seat the limit is of: a switch took the turn, as the pool was to,
        // and the limit is no longer the tool's to tell. Let go of it. (A Host that reads no
        // seats has none before or after: the same.)
        if of.seat != pooled.seat {
            held.remove(tool);
            return None;
        }
        // A hard limit is told once no other seat would take the turn, or that is not known (a
        // used-up account is never hidden); a warning only once it is known that none would.
        let warning = of.event["kind"] == "warning";
        let ready = if warning {
            pooled.near == clarp_kernel::quota_near::Failover::None
        } else {
            pooled.failover != clarp_kernel::quota_near::Failover::Spare
        };
        if !ready {
            return None;
        }
        let event = held.remove(tool)?.event;
        let resets = event["resets_at"].as_str().and_then(quota::from_text);
        if resets.is_some_and(|resets| resets <= now_ms) {
            return None;
        }
        if warning {
            clarp_kernel::quota_near::warning_event(event, pooled.near)
        } else {
            clarp_kernel::quota_near::limit_event(event, pooled.failover, pooled.regains)
        }
    }

    /// When a turn of a tool that waits for an account shows that usage is there again, given
    /// the reset its tool said (`clarp_actor::Pooled::shown_reset`): none while another seat
    /// of the tool's pool would take the turn, since the Host is moving to it and no limit
    /// holds the turn; where none would, the soonest of the tool's reset and the moment the
    /// first seat regains room. Judged for the tool's default model. A judgement that no longer
    /// holds, or none yet, leaves the tool's word.
    pub fn shown_reset(&self, tool: &str, resets_at_ms: Option<i64>, now_ms: i64) -> Option<i64> {
        let pooled = self.pooled.lock().unwrap_or_else(PoisonError::into_inner);
        let Some(pooled) = pooled.get(tool).filter(|pooled| now_ms < pooled.until_ms) else {
            return resets_at_ms;
        };
        match (pooled.failover, pooled.regains) {
            (clarp_kernel::quota_near::Failover::Spare, _) => None,
            (clarp_kernel::quota_near::Failover::None, Some(regains)) => {
                Some(resets_at_ms.map_or(regains, |reset| reset.min(regains)))
            }
            _ => resets_at_ms,
        }
    }

    /// Has the accounts looked at again when the first seat of a pool that a used-up row waits
    /// on regains room (`None`: no row waits on one), which the tool's own word does not name.
    /// Kept apart from the tools' windows, so that a look at those does not lose it.
    fn wake_by(&self, at: Option<i64>, now_ms: i64) {
        let at = at.filter(|at| *at > now_ms);
        let mut kept = self
            .pool_wake
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let sooner = at.is_some_and(|at| kept.is_none_or(|kept| kept <= now_ms || at < kept));
        *kept = at;
        drop(kept);
        if sooner {
            self.woken.notify_one();
        }
    }

    /// Whether the seats are to be read again for the moment a seat regained room: once for
    /// each such moment.
    /// Whether the seats may be read for a tool whose reading is old: once a minute at most,
    /// however often its rows ask.
    fn may_read(&self, now_ms: i64) -> bool {
        let mut asked = self
            .read_asked
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let (last, failed) = *asked;
        // Once a minute, twice as long after each read that left the reading old, to sixteen
        // minutes: a tool that keeps failing is not run every minute while a row is near.
        let every = READ_ASKED_EVERY_MS << failed.min(4);
        let due = last.is_none_or(|at| now_ms - at >= every);
        if due {
            asked.0 = Some(now_ms);
        }
        due
    }

    /// A read of the seats asked for an old reading ended: whether the reading is fresh now.
    fn read_done(&self, fresh: bool) {
        let mut asked = self
            .read_asked
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        asked.1 = if fresh { 0 } else { asked.1.saturating_add(1) };
    }

    fn read_once_for(&self, at: i64) -> bool {
        let mut read_for = self.read_for.lock().unwrap_or_else(PoisonError::into_inner);
        let first = *read_for != Some(at);
        *read_for = Some(at);
        first
    }

    /// How long until the accounts are looked at again: every five minutes, and a second
    /// after the first window that an agent's row names begins anew, or the first seat of a
    /// pool regains room, where that is sooner.
    fn next_look(&self, now_ms: i64) -> Duration {
        let wake = *self.wake.lock().unwrap_or_else(PoisonError::into_inner);
        let pool_wake = *self
            .pool_wake
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let wakes = [wake, pool_wake].into_iter().flatten();
        let until = wakes.filter(|at| *at > now_ms).min();
        let until = until.map(|at| at - now_ms + 1_000);
        let until = until.and_then(|ms| u64::try_from(ms).ok());
        until.map_or(LOOKED_EVERY, |ms| {
            Duration::from_millis(ms).min(LOOKED_EVERY)
        })
    }
}

/// What the agents' rows say of their tools' accounts (`backend_quota`): used up, and else
/// near a limit, where no other seat of the tool would take the agent's turn.
pub struct Rows {
    looked: Looked,
    accounts: clarp_kernel::recovery::Accounts,
    /// The tools whose pool has the owner's selector, which moves the tool to another seat
    /// when its account is used up.
    selectors: std::collections::BTreeSet<&'static str>,
    /// The seats as `GET /hotseat/status` serves them, where a tool is near a limit or used up.
    desk: Value,
    /// The model each tool that is near a limit or used up runs for an agent that names none.
    defaults: BTreeMap<String, String>,
}

impl Rows {
    /// For an answer that says nothing of the accounts.
    pub fn unasked() -> Self {
        Self {
            looked: Looked::default(),
            accounts: clarp_kernel::recovery::Accounts::None,
            selectors: Default::default(),
            desk: Value::Null,
            defaults: BTreeMap::new(),
        }
    }

    /// The model the tool runs for an agent: its own, else the tool's default.
    fn model<'a>(&'a self, tool: &str, model: &'a str) -> &'a str {
        if model.is_empty() {
            self.defaults.get(tool).map_or("", String::as_str)
        } else {
            model
        }
    }

    /// Whether another seat would take a turn of an agent on `model` from its tool.
    fn failover(&self, tool: &str, model: &str) -> clarp_kernel::quota_near::Failover {
        let selector = self.selectors.contains(tool);
        let model = self.model(tool, model);
        clarp_kernel::quota_near::failover(self.accounts, tool, model, selector, &self.desk)
    }

    /// Whether another seat would take a turn of an agent on `model` from its tool whose
    /// account is used up, and when the pool regains room where none would. A seats reading
    /// that is old or failed says nothing of now: the account in use then stands as used up,
    /// with its own reset (`failover: unknown`), rather than room being taken from it.
    fn used_up_failover(
        &self,
        tool: &str,
        model: &str,
    ) -> (clarp_kernel::quota_near::Failover, Option<i64>) {
        let selector = self.selectors.contains(tool);
        let model = self.model(tool, model);
        clarp_kernel::quota_near::used_up_failover(self.accounts, tool, model, selector, &self.desk)
    }

    /// What the row of an agent on this tool and model says of its account.
    pub fn of(&self, tool: &str, model: &str) -> Value {
        if let Some(used_up) = self.looked.used_up.get(tool) {
            let (failover, regains) = self.used_up_failover(tool, model);
            return clarp_kernel::quota_near::used_up_row(used_up, failover, regains);
        }
        let Some(near) = self.looked.near.get(tool) else {
            return Value::Null;
        };
        clarp_kernel::quota_near::row(near, self.failover(tool, model))
    }

    /// What the row of an agent on this tool says of the windows of the account the tool is
    /// on (`account_quota`): null where nothing is known of them, and for an agent bound to a
    /// provider of its own, which runs on another account than the tool's (issue 869).
    pub fn account(&self, tool: &str, bound: bool) -> Value {
        let account = self.looked.accounts.get(tool).filter(|_| !bound);
        account.cloned().unwrap_or(Value::Null)
    }

    /// Which tools' accounts are used up for an agent on the tool's default model: the tools
    /// whose row says so.
    pub fn used_up(&self) -> UsedUp {
        let tools = self.looked.used_up.keys();
        let rows = tools.map(|tool| (tool.clone(), self.of(tool, "")));
        rows.filter(|(_, row)| !row.is_null()).collect()
    }
}

/// What the agents' rows say of their tools' accounts now, for an app's request. The seats
/// are read as they are kept, and only where a tool is near a limit.
pub async fn rows(app: &crate::edge::App) -> Rows {
    let home = crate::config::home();
    let looked = app.limits.used_up(With::of(app, &home)).await;
    let desk = if looked.near.is_empty() && looked.used_up.is_empty() {
        Value::Null
    } else {
        crate::seats::judged(app).await
    };
    let mut defaults = BTreeMap::new();
    for tool in looked.near.keys().chain(looked.used_up.keys()) {
        let model = crate::models::default_for(app, tool).await;
        defaults.insert(tool.clone(), model.unwrap_or_default());
    }
    // A selector switches a pool this Host can judge only where it has the tool that lists the
    // seats; without one, each tool is one account, its whole pool.
    let told = app.seats.told();
    let selectors = app.pools.iter().filter(|pool| told && pool.has_selector());
    let rows = Rows {
        looked,
        accounts: app.config.accounts,
        selectors: selectors.map(|pool| pool.tool()).collect(),
        desk,
        defaults,
    };
    let asking = rows.looked.near.keys().chain(rows.looked.used_up.keys());
    for tool in asking {
        ask_reading(app, &rows, tool);
    }
    rows
}

/// Reads the seats beside whoever asked, for a tool whose reading was old, and keeps whether the
/// reading of it is fresh after: a tool that keeps failing is read less often ([`Desk::may_read`]).
fn read_for(app: &crate::edge::App, tool: &str) {
    let app = app.clone();
    let tool = tool.to_string();
    tokio::spawn(async move {
        crate::seats::read_now(&app).await;
        let desk = crate::seats::judged(&app).await;
        let fresh = clarp_kernel::quota_near::fresh_until(&tool, &desk).is_some();
        app.limits.read_done(fresh);
    });
}

/// Why a receipt of the Quota keeper is not delivered on a Host that switches the seats itself
/// (`quota_keeper`), by the rule of the agents' rows: of a tool with a pool only (`verdicts`
/// holds those), where another seat would take the turn, or, of a window that runs low, where
/// that cannot be known. `None`: it is told. A tool the Host keeps no pool of (Antigravity) is
/// told as before.
pub(crate) fn pool_cancels(
    verdicts: &BTreeMap<String, Vec<Verdicts>>,
    provider: &str,
    spent: bool,
) -> Option<&'static str> {
    use clarp_kernel::quota_near::Failover;
    // Of each model the tool runs for an agent: told where the turn of any of them would find
    // no other seat.
    let all = verdicts.get(provider).filter(|all| !all.is_empty())?;
    if all.iter().any(|verdict| verdict.tells(spent)) {
        return None;
    }
    let judged = |verdict: &Verdicts| if spent { verdict.used_up } else { verdict.near };
    Some(
        if all.iter().all(|verdict| judged(verdict) == Failover::Spare) {
            POOL_TAKES_IT
        } else {
            POOL_NOT_KNOWN
        },
    )
}

/// Why a receipt is not delivered where another seat of the tool's pool would take the turn.
pub(crate) const POOL_TAKES_IT: &str =
    "Another seat of the pool would take the turn; the Host moves to it";
/// Why a receipt that a window runs low is not delivered where whether another seat would take
/// the turn cannot be told: a warning is for a pool near its end.
pub(crate) const POOL_NOT_KNOWN: &str = "Whether another seat of the pool would take the turn could not be told; no warning is given on that";

/// Whether another seat of a tool's pool would take a turn of one model, judged now as the
/// agents' rows judge it: from an account near a limit, and from one used up.
#[derive(Debug, Clone, Copy)]
pub(crate) struct Verdicts {
    pub near: clarp_kernel::quota_near::Failover,
    pub used_up: clarp_kernel::quota_near::Failover,
}

impl Verdicts {
    /// Whether a notice of the account in use is told, as a row would say it: one that the
    /// window runs low only where no other seat would take the turn (a warning that cannot be
    /// known to hold is not given); one that nothing is left, unless another seat would take
    /// it.
    pub fn tells(self, spent: bool) -> bool {
        use clarp_kernel::quota_near::Failover;
        if spent {
            self.used_up != Failover::Spare
        } else {
            self.near == Failover::None
        }
    }
}

/// Whether another seat of a tool's pool would take a turn, for each model the tool runs for an
/// agent (its own, else the tool's default), judged now on the desk as it is kept; where the
/// reading is old, the seats are read too ([`ask_reading`]), for the next look.
pub(crate) async fn verdicts(app: &crate::edge::App, tool: &str) -> Vec<Verdicts> {
    let desk = crate::seats::judged(app).await;
    let default = crate::models::default_for(app, tool)
        .await
        .unwrap_or_default();
    // The models the tool runs for its agents: each one's own, else the tool's default.
    let directory = app.fleet.directory();
    let agents = directory.members.iter().chain(&directory.janitors);
    let mut models: std::collections::BTreeSet<String> = agents
        .filter(|member| member.record.archived_at_ms.is_none() && member.record.backend == tool)
        .map(|member| {
            let own = &member.record.model;
            if own.is_empty() {
                default.clone()
            } else {
                own.clone()
            }
        })
        .collect();
    if models.is_empty() {
        models.insert(default);
    }
    let accounts = app.config.accounts;
    // As the agents' rows: a selector counts only where the Host can read the seats.
    let selector = app.seats.told()
        && app
            .pools
            .iter()
            .any(|pool| pool.tool() == tool && pool.has_selector());
    let old = clarp_kernel::quota_near::needs_reading(accounts, tool, selector, &desk);
    if old && app.seats.told() && app.limits.may_read(app.clock.now_ms()) {
        read_for(app, tool);
    }
    models
        .iter()
        .map(|model| Verdicts {
            near: clarp_kernel::quota_near::failover(accounts, tool, model, selector, &desk),
            used_up: clarp_kernel::quota_near::used_up_failover(
                accounts, tool, model, selector, &desk,
            )
            .0,
        })
        .collect()
}

/// A tool's account is near a limit or used up, and whether another seat would take the turn
/// cannot be said, as the reading of the seats is old or not there: the seats are read now,
/// beside whoever asked, once a minute at most for the tool, and when the reading is in, the
/// rows are judged again and the apps told where that changes them ([`look_again`]). Until
/// then a near row says nothing and a used-up one stands (`failover: unknown`).
fn ask_reading(app: &crate::edge::App, rows: &Rows, tool: &str) {
    let selector = rows.selectors.contains(tool);
    let old = clarp_kernel::quota_near::needs_reading(rows.accounts, tool, selector, &rows.desk);
    // A Host told no tool that knows the seats has nothing to read them with.
    if old && app.seats.told() && app.limits.may_read(app.clock.now_ms()) {
        read_for(app, tool);
    }
}

/// The accounts are looked at again, and whether another seat would take the turns of a tool
/// that is near a limit or used up is judged again: for each model that an agent of the tool
/// runs, its own or the tool's default; and for a used-up one, when the pool regains room.
/// Where that is not as the apps were last told it, they are told to read their agents' rows
/// again. What changes it: a reading of the seats, a seat that is switched to, a reading that
/// grows old, a window of a seat that begins anew. So this is done after each reading and each
/// switch from the desk or the switcher, and by the watch.
pub async fn look_again(app: &crate::edge::App) {
    let rows = rows(app).await;
    let directory = app.fleet.directory();
    let agents = directory.members.iter().chain(&directory.janitors);
    let mut now: Told = BTreeMap::new();
    let at = app.clock.now_ms();
    let (mut regained, mut regains_first): (Option<i64>, Option<i64>) = (None, None);
    for member in agents.filter(|member| member.record.archived_at_ms.is_none()) {
        let tool = &member.record.backend;
        let model = rows.model(tool, &member.record.model).to_string();
        let judged = if rows.looked.used_up.contains_key(tool) {
            let (failover, regains) = rows.used_up_failover(tool, &model);
            // A moment that passed: a seat regained room the reading does not show yet.
            let soonest =
                |first: Option<i64>, at: i64| Some(first.map_or(at, |first| first.min(at)));
            match regains {
                Some(moment) if moment <= at => regained = soonest(regained, moment),
                Some(moment) => regains_first = soonest(regains_first, moment),
                None => {}
            }
            // To the minute, as the rows' resets are compared ([`Desk::looked`]).
            (
                failover.word(),
                regains.map(|ms| (ms + 30_000).div_euclid(60_000)),
            )
        } else if rows.looked.near.contains_key(tool) {
            (rows.failover(tool, &model).word(), None)
        } else {
            continue;
        };
        now.insert((tool.clone(), model), judged);
    }
    // A seat of the pool that regains room clears a used-up row: looked at again then, and
    // once it has, the seats are read again, as the reading before does not show it.
    app.limits.wake_by(regains_first, at);
    if let Some(moment) = regained
        && app.limits.read_once_for(moment)
    {
        crate::seats::read_again(app);
    }
    judge_pools(app, &rows).await;
    let mut told = app
        .limits
        .failover
        .lock()
        .unwrap_or_else(PoisonError::into_inner);
    let changed = *told != now;
    *told = now;
    drop(told);
    if changed {
        let again = json!({ "type": "agent-roster", "kind": "backend-quota" });
        app.hub.publish(again, app.clock.now_ms());
    }
}

/// Judges for each tool whose limits are told whether another seat of its pool would take a turn
/// on its default model from an account that is used up, and keeps it for the tool's hard
/// limits ([`Desk::pooled`]): until the reading of the seats grows old, and five minutes at
/// most, as the watch judges it again by then.
async fn judge_pools(app: &crate::edge::App, rows: &Rows) {
    let now = app.clock.now_ms();
    let desk = if rows.desk.is_null() {
        crate::seats::judged(app).await
    } else {
        rows.desk.clone()
    };
    let mut judged = BTreeMap::new();
    for tool in [CODEX, CLAUDE] {
        let model = crate::models::default_for(app, tool)
            .await
            .unwrap_or_default();
        let selector = rows.selectors.contains(tool);
        let pooled = judge(rows.accounts, (tool, &model), selector, &desk, now);
        judged.insert(tool.to_string(), pooled);
    }
    let releases: Vec<Value> = judged
        .iter()
        .filter_map(|(tool, pooled)| app.limits.release(tool, pooled, now))
        .collect();
    *app.limits
        .pooled
        .lock()
        .unwrap_or_else(PoisonError::into_inner) = judged;
    // A hard limit the pool was to take, which it can no longer: told now.
    for event in releases {
        app.hub.publish(event, now);
    }
}

/// Whether another seat of a tool's pool would take a turn of `model` from its used-up account,
/// by the desk the Host judges by (`seats::judged`), and until when that holds: until the reading
/// of the seats grows old, and five minutes at most; with the seat the tool is on.
fn judge(
    accounts: clarp_kernel::recovery::Accounts,
    (tool, model): (&str, &str),
    selector: bool,
    desk: &Value,
    now: i64,
) -> Pooled {
    let (failover, regains) =
        clarp_kernel::quota_near::used_up_failover(accounts, tool, model, selector, desk);
    let near = clarp_kernel::quota_near::failover(accounts, tool, model, selector, desk);
    let most = now + i64::try_from(LOOKED_EVERY.as_millis()).unwrap_or(i64::MAX);
    let fresh = clarp_kernel::quota_near::fresh_until(tool, desk);
    Pooled {
        failover,
        near,
        regains,
        until_ms: fresh.map_or(most, |fresh| fresh.min(most)),
        seat: clarp_kernel::quota_near::seat_in_use(tool, desk),
    }
}

/// That an account is used up or near a limit stops being so with time, and nothing happens
/// when it does: what was said of it grows old, its window begins anew; and so does whether
/// another seat would take a turn, as the reading of the seats grows old. So it is looked at
/// now and then (every five minutes, as the Python Host looks, and a second after the first
/// window an agent's row names begins anew), and an app that shows such an account is told to
/// look again.
pub fn watch(app: &crate::edge::App) {
    let app = app.clone();
    tokio::spawn(async move {
        // Once at the start: what the tools' limits are told by is judged from the first.
        look_again(&app).await;
        loop {
            let wait = app.limits.next_look(app.clock.now_ms());
            tokio::select! {
                () = tokio::time::sleep(wait) => {}
                // A window named sooner than the one waited for: wait for that one.
                () = app.limits.woken.notified() => continue,
            }
            look_again(&app).await;
        }
    });
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
            fleet.keep_said(KIND, tool.to_string(), Some(written)).await;
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
            fleet
                .keep_said(KIND, CLAUDE.to_string(), Some(written))
                .await;
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

/// When a turn that completed is taken to have completed: when it ended, no later than now,
/// and after the word of its account's windows it gave in the turn, where it gave one (that
/// word no later than now). A turn ends at its tool's own end line (issue 750), which can come
/// in the same millisecond as that word: the word was still said before the turn was over.
fn completed_at(ended_ms: i64, its_word_ms: Option<i64>, now_ms: i64) -> i64 {
    let at_ms = ended_ms.min(now_ms);
    its_word_ms.map_or(at_ms, |said| at_ms.max(said + 1))
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

    /// An agent's row says of its tool's account the five hours and the week, as the usage
    /// screen is shown them, its resets in UTC to the second; nothing where neither is known,
    /// and nothing to an agent bound to a provider of its own (issue 869). The apps are told
    /// again as a window's whole percent or its reset's minute moves, not at every word.
    #[test]
    fn a_row_says_the_windows_of_its_tools_account() {
        let provider = |used: f64, resets: &str| {
            json!({
                "provider_id": "codex", "freshness": "fresh",
                "observed_at": "2026-11-26T10:00:00Z",
                "windows": [
                    {"kind": "five_hour", "used_percentage": used, "resets_at": resets},
                    {"kind": "seven_day", "used_percentage": 12.0, "resets_at": null},
                    {"kind": "unknown", "used_percentage": null, "resets_at": null},
                ],
            })
        };
        let account = account_of(&provider(41.5, "2026-11-26T12:00:00.400+00:00")).unwrap();
        assert_eq!(
            account,
            json!({
                "provider_id": "codex", "freshness": "fresh",
                "observed_at": "2026-11-26T10:00:00Z",
                "windows": [
                    {"kind": "five_hour", "used_percentage": 41.5, "resets_at": "2026-11-26T12:00:00Z"},
                    {"kind": "seven_day", "used_percentage": 12.0, "resets_at": null},
                ],
            })
        );
        let none = json!({"provider_id": "codex", "freshness": "unknown", "windows": []});
        assert_eq!(account_of(&none), None);
        let shown = |used, resets| account_shown(account_of(&provider(used, resets)).as_ref());
        let at = "2026-11-26T12:00:00Z";
        assert_eq!(shown(41.5, at), shown(41.9, "2026-11-26T12:00:20Z"));
        assert_ne!(shown(41.5, at), shown(42.0, at));
        assert_ne!(shown(41.5, at), shown(41.5, "2026-11-26T12:05:00Z"));
        let mut accounts = UsedUp::new();
        accounts.insert("codex".into(), account.clone());
        let rows = Rows {
            looked: Looked {
                accounts: Arc::new(accounts),
                ..Looked::default()
            },
            ..Rows::unasked()
        };
        assert_eq!(rows.account("codex", false), account);
        assert_eq!(rows.account("codex", true), Value::Null);
        assert_eq!(rows.account("claude", false), Value::Null);
    }

    #[test]
    fn a_waiting_turn_and_a_hard_limit_are_told_by_the_pool_while_its_judgement_holds() {
        use clarp_kernel::quota_near::Failover;
        let desk = Desk::default();
        let judged = |failover, regains, until_ms| {
            let pooled = Pooled {
                failover,
                near: failover,
                regains,
                until_ms,
                seat: Some("a".to_string()),
            };
            let mut all = desk.pooled.lock().unwrap();
            all.insert(CODEX.to_string(), pooled);
        };
        let hard =
            json!({ "type": "provider-limit", "kind": "hard_limit", "provider_id": "codex" });
        // Not judged yet: the tool's word, and the limit told as not known.
        assert_eq!(desk.shown_reset(CODEX, Some(9_000), 0), Some(9_000));
        let told = desk.pooled(CODEX, hard.clone(), 0).unwrap();
        assert_eq!(told["failover"], "unknown");
        // Another seat would take the turn: no reset shown, no hard limit told.
        judged(Failover::Spare, None, 1_000);
        assert_eq!(desk.shown_reset(CODEX, Some(9_000), 0), None);
        assert!(desk.pooled(CODEX, hard.clone(), 0).is_none());
        // Once the judgement no longer holds, it hides nothing.
        assert_eq!(desk.shown_reset(CODEX, Some(9_000), 1_000), Some(9_000));
        assert!(desk.pooled(CODEX, hard.clone(), 1_000).is_some());
        // No seat has room: the first that regains it, where that is sooner.
        judged(Failover::None, Some(5_000), 1_000);
        assert_eq!(desk.shown_reset(CODEX, Some(9_000), 0), Some(5_000));
        assert_eq!(desk.shown_reset(CODEX, Some(4_000), 0), Some(4_000));
        assert_eq!(desk.shown_reset(CODEX, None, 0), Some(5_000));
        let told = desk.pooled(CODEX, hard, 0).unwrap();
        assert_eq!(told["failover"], "none");
        assert_eq!(told["pool_regains_at"], quota::moment(5_000));
        // Another tool's turns are not judged by it.
        assert_eq!(desk.shown_reset(CLAUDE, Some(9_000), 0), Some(9_000));
    }

    #[test]
    fn a_hard_limit_the_pool_took_is_told_once_the_pool_cannot_take_the_turn() {
        use clarp_kernel::quota_near::Failover;
        let desk = Desk::default();
        let pooled = |failover, regains, seat: &str| Pooled {
            failover,
            near: failover,
            regains,
            until_ms: 1_000,
            seat: Some(seat.to_string()),
        };
        let hold = |seat: &str| {
            desk.pooled
                .lock()
                .unwrap()
                .insert(CODEX.to_string(), pooled(Failover::Spare, None, seat));
        };
        let hard = json!({ "type": "provider-limit", "kind": "hard_limit", "provider_id": "codex",
            "resets_at": quota::moment(9_000_000) });
        // Seat A hits its limit while B has room: not told, held as A's.
        hold("a");
        assert!(desk.pooled(CODEX, hard.clone(), 0).is_none());
        // While the tool stays on A and the pool still takes it, nothing; once it cannot, the
        // limit, told once.
        assert!(
            desk.release(CODEX, &pooled(Failover::Spare, None, "a"), 0)
                .is_none()
        );
        let told = desk
            .release(CODEX, &pooled(Failover::None, Some(5_000), "a"), 0)
            .unwrap();
        assert_eq!(
            (&told["kind"], &told["failover"], &told["pool_regains_at"]),
            (
                &json!("hard_limit"),
                &json!("none"),
                &json!(quota::moment(5_000))
            )
        );
        assert!(
            desk.release(CODEX, &pooled(Failover::None, None, "a"), 0)
                .is_none()
        );
        // The switch took the turn to B: judged with B as the seat in use, and B taken as used
        // up, no seat is left; yet the limit was A's, and the tool left it. Nothing is told, and
        // nothing is held any more, also once the tool is back on A.
        hold("a");
        assert!(desk.pooled(CODEX, hard.clone(), 0).is_none());
        assert!(
            desk.release(CODEX, &pooled(Failover::None, None, "b"), 0)
                .is_none()
        );
        assert!(
            desk.release(CODEX, &pooled(Failover::None, None, "a"), 0)
                .is_none()
        );
        // A limit that is over is let go, and one whose window began anew is not told.
        hold("a");
        assert!(desk.pooled(CODEX, hard.clone(), 0).is_none());
        let over = json!({ "type": "provider-limit", "kind": "recovered", "provider_id": "codex" });
        assert!(desk.pooled(CODEX, over, 0).is_some());
        assert!(
            desk.release(CODEX, &pooled(Failover::None, None, "a"), 0)
                .is_none()
        );
        assert!(desk.pooled(CODEX, hard, 0).is_none());
        assert!(
            desk.release(CODEX, &pooled(Failover::Unknown, None, "a"), 9_000_000)
                .is_none()
        );
    }

    #[test]
    fn a_hard_limit_is_held_and_told_through_the_seats_as_a_host_reads_and_switches_them() {
        use clarp_kernel::quota_near::Failover;
        use clarp_kernel::recovery::Accounts;
        use clarp_kernel::seats::Seats;
        // The seats as the owner's tool lists them, read and kept as a Host keeps them; and the
        // desk judged by, as the Host makes it (`seats::judged`).
        let at = 1_899_999_000_000;
        let usage = |used: f64, reset_s: i64| {
            json!({ "status": "allowed", "limited": used >= 1.0, "used_5h": used,
                "reset_5h": reset_s, "used_7d": 0.2, "reset_7d": 1_900_400_000, "scoped": [] })
        };
        let list = |a: f64, b: f64| {
            json!({ "accounts": [
                { "alias": "a", "is_active": true, "usage": usage(a, 1_900_007_200) },
                { "alias": "b", "is_active": false, "usage": usage(b, 1_900_003_600) },
            ]})
        };
        let judged = |seats: &Seats| {
            let desk = seats.judged(true, at);
            judge(Accounts::Select, (CLAUDE, ""), true, &desk, at)
        };
        let hard = json!({ "type": "provider-limit", "kind": "hard_limit", "provider_id": "claude",
            "resets_at": quota::moment(1_900_007_200_000) });
        let hold = |desk: &Desk, seats: &Seats| {
            let pooled = judged(seats);
            assert_eq!(pooled.failover, Failover::Spare, "{pooled:?}");
            assert!(pooled.seat.is_some(), "{pooled:?}");
            desk.pooled
                .lock()
                .unwrap()
                .insert(CLAUDE.to_string(), pooled);
            assert!(desk.pooled(CLAUDE, hard.clone(), at).is_none());
        };

        // Seat a is used up, b has room: the limit is held, as a's.
        let desk = Desk::default();
        let mut seats = Seats::with_salt("a-salt");
        seats.read(CLAUDE, &list(1.0, 0.1), at);
        hold(&desk, &seats);
        // The switch never came, and b filled up too: no seat has room, and the tool is still on
        // a. The limit is told, with when the first seat regains room (b, in an hour).
        let mut stayed = Seats::with_salt("a-salt");
        stayed.read(CLAUDE, &list(1.0, 1.0), at);
        let pooled = judged(&stayed);
        assert_eq!(
            (pooled.failover, pooled.regains),
            (Failover::None, Some(1_900_003_600_000))
        );
        let told = desk.release(CLAUDE, &pooled, at).unwrap();
        assert_eq!(
            (&told["failover"], &told["pool_regains_at"]),
            (&json!("none"), &json!(quota::moment(1_900_003_600_000)))
        );

        // Held again; this time the Host switches to b. Judged on b, with b taken as used up,
        // no seat is left, but the limit was a's and the tool left a: nothing is told, then or
        // later.
        hold(&desk, &seats);
        seats.mark_active(CLAUDE, "b");
        let pooled = judged(&seats);
        assert_eq!(pooled.failover, Failover::None, "{pooled:?}");
        assert!(desk.release(CLAUDE, &pooled, at).is_none());
        assert!(desk.release(CLAUDE, &judged(&stayed), at).is_none());
    }

    #[test]
    fn a_warning_is_told_through_the_seats_only_where_the_pool_is_near_its_end() {
        use clarp_kernel::quota_near::Failover;
        use clarp_kernel::recovery::Accounts;
        use clarp_kernel::seats::Seats;
        // The seats as the owner's tool lists them, read and kept as a Host keeps them, and the
        // desk judged by as the Host makes it (`seats::judged`).
        let at = 1_899_999_000_000;
        let usage = |five: f64| {
            json!({ "status": "allowed", "limited": false, "used_5h": five,
                "reset_5h": 1_900_007_200, "used_7d": 0.2, "reset_7d": 1_900_400_000, "scoped": [] })
        };
        let list = |a: f64, b: f64| {
            json!({ "accounts": [
                { "alias": "a", "is_active": true, "usage": usage(a) },
                { "alias": "b", "is_active": false, "usage": usage(b) },
            ]})
        };
        let read = |a: f64, b: f64| {
            let mut seats = Seats::with_salt("a-salt");
            seats.read(CLAUDE, &list(a, b), at);
            seats
        };
        let judged = |seats: &Seats, now: i64| {
            let desk = seats.judged(true, now);
            judge(Accounts::Select, (CLAUDE, ""), true, &desk, now)
        };
        let warning = json!({ "type": "provider-limit", "kind": "warning", "provider_id": "claude",
            "resets_at": quota::moment(1_900_007_200_000) });
        let desk = Desk::default();
        let set = |pooled: Pooled| {
            desk.pooled
                .lock()
                .unwrap()
                .insert(CLAUDE.to_string(), pooled);
        };

        // Seat a at 85% of its five hours, b with room: another seat would take the turn, so the
        // warning is not told, and is held as a's.
        let roomy = read(0.85, 0.1);
        let pooled = judged(&roomy, at);
        assert_eq!(pooled.near, Failover::Spare, "{pooled:?}");
        set(pooled);
        assert!(desk.pooled(CLAUDE, warning.clone(), at).is_none());
        assert!(desk.release(CLAUDE, &judged(&roomy, at), at).is_none());
        // b comes near its end too, and the tool is still on a: the pool is near its end, and the
        // warning is told, with failover none.
        let near_end = judged(&read(0.85, 0.9), at);
        assert_eq!(near_end.near, Failover::None, "{near_end:?}");
        let told = desk.release(CLAUDE, &near_end, at).unwrap();
        assert_eq!(
            (&told["kind"], &told["failover"]),
            (&json!("warning"), &json!("none"))
        );
        // A warning while that is known: told at once.
        set(judged(&read(0.85, 0.9), at));
        let told = desk.pooled(CLAUDE, warning.clone(), at).unwrap();
        assert_eq!(told["failover"], "none");

        // The reading of the seats is old: whether another seat would take the turn is not known,
        // and no warning is given, now or while that stays so.
        let old = judged(&read(0.85, 0.9), at + 2 * 3_600_000);
        assert_eq!(old.near, Failover::Unknown, "{old:?}");
        set(old.clone());
        assert!(desk.pooled(CLAUDE, warning, at + 2 * 3_600_000).is_none());
        assert!(desk.release(CLAUDE, &old, at + 2 * 3_600_000).is_none());
    }

    #[test]
    fn the_keeper_judges_only_the_tools_with_a_pool_by_it() {
        use clarp_kernel::quota_near::Failover;
        let verdict = |near, used_up| Verdicts { near, used_up };
        let one = |tool: &str, near, used_up| -> BTreeMap<String, Vec<Verdicts>> {
            [(tool.to_string(), vec![verdict(near, used_up)])].into()
        };
        // A tool the Host keeps no pool of: told, runs low or spent.
        for spent in [false, true] {
            let any = one(CLAUDE, Failover::Spare, Failover::Spare);
            assert_eq!(pool_cancels(&any, "agy", spent), None);
        }
        // Not known: a warning is not given; a spent window is.
        let unknown = one(CLAUDE, Failover::Unknown, Failover::Unknown);
        assert_eq!(pool_cancels(&unknown, CLAUDE, false), Some(POOL_NOT_KNOWN));
        assert_eq!(pool_cancels(&unknown, CLAUDE, true), None);
        // Another seat would take the turn: neither.
        let spare = one(CODEX, Failover::Spare, Failover::Spare);
        assert_eq!(pool_cancels(&spare, CODEX, false), Some(POOL_TAKES_IT));
        assert_eq!(pool_cancels(&spare, CODEX, true), Some(POOL_TAKES_IT));
        // No seat would: both are told.
        let none = one(CODEX, Failover::None, Failover::None);
        assert_eq!(pool_cancels(&none, CODEX, false), None);
        assert_eq!(pool_cancels(&none, CODEX, true), None);
        // Each agent's model: told where the turn of any one of them would find no seat; where
        // one would move and another's cannot be told, what is known is that it is not known.
        let both = |first, second| -> BTreeMap<String, Vec<Verdicts>> {
            let all = vec![verdict(first, first), verdict(second, second)];
            [(CLAUDE.to_string(), all)].into()
        };
        let one_left = both(Failover::Spare, Failover::None);
        assert_eq!(pool_cancels(&one_left, CLAUDE, false), None);
        let mixed = both(Failover::Spare, Failover::Unknown);
        assert_eq!(pool_cancels(&mixed, CLAUDE, false), Some(POOL_NOT_KNOWN));
        assert_eq!(
            pool_cancels(&both(Failover::Spare, Failover::Spare), CLAUDE, false),
            Some(POOL_TAKES_IT)
        );
    }

    #[test]
    fn the_seats_are_read_for_an_old_reading_once_a_minute_and_less_often_while_it_stays_old() {
        let desk = Desk::default();
        let minute = 60_000;
        assert!(desk.may_read(0));
        // One read for every tool: a second ask in the same minute reads nothing.
        assert!(!desk.may_read(minute - 1));
        assert!(desk.may_read(minute));
        // Reads that leave the reading old: two minutes, then four, to sixteen at most.
        desk.read_done(false);
        assert!(!desk.may_read(2 * minute + minute - 1));
        assert!(desk.may_read(3 * minute));
        desk.read_done(false);
        assert!(!desk.may_read(3 * minute + 4 * minute - 1));
        assert!(desk.may_read(7 * minute));
        for _ in 0..10 {
            desk.read_done(false);
        }
        assert!(!desk.may_read(7 * minute + 16 * minute - 1));
        assert!(desk.may_read(23 * minute));
        // A read that makes it fresh: once a minute again.
        desk.read_done(true);
        assert!(desk.may_read(24 * minute));
    }

    #[test]
    fn a_warning_held_while_the_reading_is_old_is_told_after_the_next_read_through_the_seats() {
        use clarp_kernel::quota_near::Failover;
        use clarp_kernel::recovery::Accounts;
        use clarp_kernel::seats::Seats;
        // The seats as the owner's tool lists them, read and kept as a Host keeps them, and the
        // desk judged by as the Host makes it (`seats::judged`): a at 85% of its five hours, the
        // seat in use; b at 90%, no room to spare.
        let at = 1_899_999_000_000;
        let usage = |five: f64| {
            json!({ "status": "allowed", "limited": false, "used_5h": five,
                "reset_5h": 1_900_007_200, "used_7d": 0.2, "reset_7d": 1_900_400_000, "scoped": [] })
        };
        let list = json!({ "accounts": [
            { "alias": "a", "is_active": true, "usage": usage(0.85) },
            { "alias": "b", "is_active": false, "usage": usage(0.9) },
        ]});
        let read_at = |when: i64| {
            let mut seats = Seats::with_salt("a-salt");
            seats.read(CLAUDE, &list, when);
            seats
        };
        let judged = |seats: &Seats, now: i64| {
            let desk = seats.judged(true, now);
            judge(Accounts::Select, (CLAUDE, ""), true, &desk, now)
        };
        let warning = json!({ "type": "provider-limit", "kind": "warning", "provider_id": "claude",
            "resets_at": quota::moment(1_900_007_200_000) });
        let desk = Desk::default();
        // Judged on a; then twenty minutes pass with no reading: the judgement no longer holds.
        let first = judged(&read_at(at), at);
        assert!(first.seat.is_some(), "{first:?}");
        desk.pooled
            .lock()
            .unwrap()
            .insert(CLAUDE.to_string(), first);
        let later = at + 20 * 60_000;
        // The warning comes now: not known whether another seat would take the turn, so it is
        // not told, and it is held as a's.
        assert!(desk.pooled(CLAUDE, warning, later).is_none());
        // The seats are read again, the tool still on a, and no seat has room to spare: told,
        // with failover none.
        let again = judged(&read_at(later), later);
        assert_eq!(again.near, Failover::None, "{again:?}");
        let told = desk.release(CLAUDE, &again, later).unwrap();
        assert_eq!(
            (&told["kind"], &told["failover"]),
            (&json!("warning"), &json!("none"))
        );
    }

    #[test]
    fn a_limit_held_before_any_seat_was_judged_is_of_the_first_seat_judged() {
        use clarp_kernel::quota_near::Failover;
        let desk = Desk::default();
        let warning = json!({ "type": "provider-limit", "kind": "warning", "provider_id": "codex",
            "resets_at": quota::moment(9_000_000) });
        // Nothing judged yet: held, of no seat.
        assert!(desk.pooled(CODEX, warning, 0).is_none());
        let pooled = |near, seat: &str| Pooled {
            failover: near,
            near,
            regains: None,
            until_ms: 9_000,
            seat: Some(seat.to_string()),
        };
        // The first judgement: on a, and another seat would take the turn. Not told, and now
        // held as a's.
        assert!(
            desk.release(CODEX, &pooled(Failover::Spare, "a"), 1_000)
                .is_none()
        );
        // On a still, and no seat would: told, with failover none.
        let told = desk
            .release(CODEX, &pooled(Failover::None, "a"), 2_000)
            .unwrap();
        assert_eq!(
            (&told["kind"], &told["failover"]),
            (&json!("warning"), &json!("none"))
        );
    }

    #[test]
    fn a_warning_that_comes_while_the_judgement_is_old_is_of_the_seat_last_judged() {
        use clarp_kernel::quota_near::Failover;
        let desk = Desk::default();
        let pooled = |near, until_ms| Pooled {
            failover: near,
            near,
            regains: None,
            until_ms,
            seat: Some("a".to_string()),
        };
        // Judged on seat a; that judgement no longer holds when the warning comes.
        desk.pooled
            .lock()
            .unwrap()
            .insert(CODEX.to_string(), pooled(Failover::None, 1_000));
        let warning = json!({ "type": "provider-limit", "kind": "warning", "provider_id": "codex",
            "resets_at": quota::moment(9_000_000) });
        // Not known now: not told, held as a's.
        assert!(desk.pooled(CODEX, warning, 2_000).is_none());
        // The seats are read again, the tool still on a, and no seat would take the turn: told.
        let told = desk
            .release(CODEX, &pooled(Failover::None, 9_000), 3_000)
            .unwrap();
        assert_eq!(
            (&told["kind"], &told["failover"]),
            (&json!("warning"), &json!("none"))
        );
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
    fn a_turn_completes_after_the_word_of_its_account_it_gave() {
        // Its end line in the same millisecond as its word: after the word all the same.
        assert_eq!(completed_at(1_000, Some(1_000), 5_000), 1_001);
        // An end after its word, or a turn that gave none: when it ended.
        assert_eq!(completed_at(1_200, Some(1_000), 5_000), 1_200);
        assert_eq!(completed_at(1_000, None, 5_000), 1_000);
        // A clock ahead: no later than now, but for the moment after its word.
        assert_eq!(completed_at(9_000, None, 5_000), 5_000);
        assert_eq!(completed_at(9_000, Some(5_000), 5_000), 5_001);
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
