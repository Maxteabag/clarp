//! What is left of a tool's account, as the tool itself said it: read, kept and shown.
//!
//! Codex writes how much of each window of its account is used, and when the window begins
//! anew, into its conversation's rollout with every count of tokens. When a turn of Codex
//! ends, the newest such record of the turn's rollout is read here
//! (`clarp_backend::limits`), kept for the tool (`clarp_kernel::quota`), and what follows
//! from it is told to the apps: a warning at four fifths of the five-hour window, a hard
//! limit, and when either is over. An app's usage screen is shown what is kept
//! (`GET /backend-usage`). Nobody is asked anything: a Host whose agents run no turn learns
//! nothing new, and what it shows says how old its word is.
//!
//! An agent's row says of its tool that its account is used up (`backend_quota`) while a
//! window that was said to be all used is fresh and no turn of the tool completed since:
//! advice for whoever is about to write to the agent, and no refusal.
//!
//! A turn that fails for a usage limit is a limit too, of which nothing more is known: it is
//! told as a hard limit and stands until a turn of the tool completes.
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

use clarp_kernel::quota::{self, Kept, Login, Shown, Tool};
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
    /// Which accounts were used up when that was last looked at: to tell when it is no
    /// longer as it was.
    last: std::sync::Mutex<Option<Arc<UsedUp>>>,
}

impl Desk {
    /// A turn of a tool ended, in the tool's own session: what the tool wrote of its
    /// account's limits is read, kept, and what follows from it told, and so is what
    /// follows from how the turn ended. A tool that writes no such record, and a turn that
    /// wrote none, leave the windows as they are.
    pub async fn turn_ended(&self, with: With<'_>, tool: &str, session: &str, how: Ending) {
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
        self.looked(with, tool, &kept, on.as_ref());
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
        self.looked(with, tool, &kept, on.as_ref());
        shown
    }

    /// Which accounts are used up now: looked at afresh.
    pub async fn used_up(&self, with: With<'_>) -> Arc<UsedUp> {
        let mut known = self.known.lock().await;
        let on = known.login(with, CODEX, false).await;
        let kept = known.of(with.fleet, CODEX).await.unwrap_or_default();
        self.looked(with, CODEX, &kept, on.as_ref())
    }

    /// Looks whether a tool's account is used up, and remembers it. Where an app would show
    /// another thing of it than at the last look (which window, until when), the apps are
    /// told to read their agents' rows again. The first look tells nothing: nothing is
    /// known to have changed.
    fn looked(&self, with: With<'_>, tool: &str, kept: &Kept, on: Option<&Named>) -> Arc<UsedUp> {
        let instance = format!("{}:{tool}", with.server_id);
        let of = Tool {
            instance: &instance,
            provider: tool,
        };
        let now = kept.used_up(of, on.map(Named::login), with.now_ms);
        let shown = |all: &UsedUp| {
            let said = all.get(tool)?;
            Some((said["window"].clone(), said["resets_at"].clone()))
        };
        let mut last = self.last.lock().unwrap_or_else(PoisonError::into_inner);
        let mut all: UsedUp = last.as_deref().cloned().unwrap_or_default();
        match now {
            Some(said) => all.insert(tool.to_string(), said),
            None => all.remove(tool),
        };
        let changed = last
            .as_deref()
            .is_some_and(|before| shown(before) != shown(&all));
        let all = Arc::new(all);
        *last = Some(all.clone());
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
    let of = |kind: &str, what: String| {
        let digest: String = keyed(key, what.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        format!("{kind}-{}", &digest[..24])
    };
    Some(Named {
        generation: of("auth", format!("auth\0{account}\0{token}")),
        account: of("account", format!("account\0{account}")),
        since_ms,
    })
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
}
