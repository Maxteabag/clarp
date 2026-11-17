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
//! Whose account a record is of is told by the login the tool is on when the record is
//! read. The file the tool keeps its login in is read for the account and the token, and of
//! both only a keyed digest is kept (the key is this Host's own, drawn once), so that
//! neither can be read back or looked for in what the Host keeps. A record that was
//! written before the login file was last written is of nobody this Host can name, and is
//! not kept.

use std::io::{Read, Seek, SeekFrom};
use std::path::Path;
use std::time::UNIX_EPOCH;

use clarp_kernel::quota::{self, Kept, Login, Shown, Tool};
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

/// What the tools said of their accounts. One at a time reads what is kept, changes it and
/// writes it back.
#[derive(Default)]
pub struct Desk {
    held: tokio::sync::Mutex<()>,
}

impl Desk {
    /// A turn of a tool ended, in the tool's own session: what the tool wrote of its
    /// account's limits is read, kept, and what follows from it told. A tool that writes no
    /// such record, and a turn that wrote none, leave everything as it is.
    pub async fn turn_ended(&self, with: With<'_>, tool: &str, session: &str) {
        if tool != CODEX || session.is_empty() {
            return;
        }
        let sessions = crate::past::codex_home(with.home).join("sessions");
        let named = session.to_string();
        let read = tokio::task::spawn_blocking(move || {
            let rollout = clarp_backend::past::codex_rollout(&sessions, &named)?;
            newest_record(&rollout)
        });
        let Ok(Some((record, written_ms))) = read.await else {
            return;
        };
        let Ok(snapshot) = clarp_backend::limits::codex(&json!({ "rate_limit": record })) else {
            return;
        };
        let _held = self.held.lock().await;
        let Some(on) = named_login(with, tool, true).await else {
            return;
        };
        let mut kept = kept(with.fleet, tool).await;
        let Some(at_ms) = taken(written_ms, on.since_ms, kept.seen_at_ms(), with.now_ms) else {
            return;
        };
        let instance = format!("{}:{tool}", with.server_id);
        let of = Tool {
            instance: &instance,
            provider: tool,
        };
        let events = kept.seen(of, on.login(), &snapshot, SEEN_IN, at_ms, &mut drawn);
        keep(with.fleet, tool, &kept).await;
        for event in events {
            with.hub.publish(event, with.now_ms);
        }
    }

    /// What an app's usage screen is shown of a tool, on the login the tool is on now. A
    /// limit whose window began anew since it was told is told as no longer known, to this
    /// app in the answer and to the others as it is told anything.
    pub async fn asked(&self, with: With<'_>, tool: &str) -> Shown {
        let _held = self.held.lock().await;
        let on = named_login(with, tool, false).await;
        let mut kept = kept(with.fleet, tool).await;
        let instance = format!("{}:{tool}", with.server_id);
        let of = Tool {
            instance: &instance,
            provider: tool,
        };
        let login = on.as_ref().map(Named::login);
        let shown = kept.asked(of, login, with.now_ms, &mut drawn);
        if !shown.events.is_empty() {
            keep(with.fleet, tool, &kept).await;
            for event in &shown.events {
                with.hub.publish(event.clone(), with.now_ms);
            }
        }
        shown
    }
}

/// An id nobody has, after its prefix.
fn drawn(prefix: &str) -> String {
    format!("{prefix}{}", crate::ids::uuid())
}

/// What is kept of a tool's account: nothing, where nothing was kept or it cannot be read.
async fn kept(fleet: &FleetHandle, tool: &str) -> Kept {
    let written = fleet.kept(KIND, tool.to_string()).await.ok().flatten();
    written
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default()
}

async fn keep(fleet: &FleetHandle, tool: &str, kept: &Kept) {
    if let Ok(written) = serde_json::to_vec(kept) {
        let _ = fleet.keep(KIND, tool.to_string(), Some(written)).await;
    }
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

/// The login a tool is on, named with this Host's key. `None`: the tool is on no login that
/// has an account's windows (no file, a key of the provider's in place of a login, no
/// account in it), or there is no key yet and none is to be drawn (`draw`: nothing was ever
/// kept then, and nothing is shown).
async fn named_login(with: With<'_>, tool: &str, draw: bool) -> Option<Named> {
    let file = clarp_backend::by_id(tool)?.login_file(with.home)?;
    // The key first: a Host that never kept a record has none, and reads no login.
    let key = match with.fleet.kept(KIND, KEY.to_string()).await.ok()? {
        Some(key) => key,
        None if draw => {
            let key = crate::ids::hex(32).into_bytes();
            with.fleet
                .keep(KIND, KEY.to_string(), Some(key.clone()))
                .await
                .ok()?;
            key
        }
        None => return None,
    };
    let read = tokio::task::spawn_blocking(move || {
        let written = std::fs::metadata(&file).and_then(|of| of.modified()).ok()?;
        let since = written.duration_since(UNIX_EPOCH).ok()?;
        Some((std::fs::read(&file).ok()?, since.as_millis() as i64))
    });
    let (bytes, since_ms) = read.await.ok().flatten()?;
    named(&bytes, &key, since_ms)
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
