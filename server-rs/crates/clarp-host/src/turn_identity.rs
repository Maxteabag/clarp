//! Who is calling: the token this Host gives every turn of an agent, signed with a key of its own.
//!
//! When a turn's tool is about to be started, the token of that turn ([`Signer::token`]) is put
//! into its environment ([`TURN_ENV`]). It says whose turn it is and is signed: nothing is written
//! for it, and nothing per turn is kept. What a token is and when it names its agent is the
//! kernel's rule ([`clarp_kernel::turn_identity`]); this keeps the key and asks the agent's own log
//! where the turn stands.
//!
//! The key is 32 random bytes, made once and kept as one record with the fleet (kind [`KIND`]),
//! read when the Host starts. It is never written to a log line and no route answers it. A Host
//! that cannot read it gives no turn a token, and answers a token presented that it cannot say
//! whose it is: a 503 the caller may try again, as the Python Host answers a lookup that failed.
//!
//! What one verification reads: the key, from memory (from the fleet's store only where it could
//! not be read at the start), and one question to the agent's actor (two keyed reads of its log:
//! the turn that runs, and the row that ended this one).

use std::sync::{Arc, RwLock};

use axum::http::StatusCode;
use clarp_kernel::turn_identity::{self, KEY_BYTES};
use clarp_ports::kv::KeyValue;
use ring::rand::{SecureRandom, SystemRandom};
use serde::{Deserialize, Serialize};

pub use clarp_kernel::turn_identity::TURN_ENV;

use crate::agent::Unavailable;
use crate::edge::{ApiError, App};
use crate::fleet::{FleetHandle, Member};

/// The kind of record the key is, among what is kept with the fleet.
pub const KIND: &str = "turn-identity";
/// The key of the one record of that kind.
pub const KEY: &str = "key";
/// The header a request presents its turn's token in.
pub const HEADER: &str = "x-clarp-turn";
/// What a caller is answered when this Host cannot say whose turn a token is.
pub const UNAVAILABLE: &str = "turn identity unavailable";

type Key = [u8; KEY_BYTES];

/// The record the key is kept as.
#[derive(Serialize, Deserialize)]
struct Kept {
    /// The key, in lowercase hex.
    key: String,
}

/// The Host's key, and what makes and reads tokens with it. Shows nothing of the key.
#[derive(Default)]
pub struct Signer {
    key: RwLock<Option<Key>>,
    /// Why a key made at the start could not be kept, in the store's words.
    unkept: Option<String>,
}

impl std::fmt::Debug for Signer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Signer")
    }
}

impl Signer {
    /// The Host's key, read from its store, or made and kept there where there is none yet. A key
    /// that is there but cannot be read is never replaced: every token given with it would name
    /// nobody. Without one no turn is given a token.
    pub fn open(store: &mut dyn KeyValue) -> Signer {
        let table = crate::fleet::shelf(KIND);
        let (key, unkept) = match store.get(&table, KEY) {
            Ok(Some(kept)) => (key_of(&kept), None),
            Ok(None) => match made() {
                // A key that is not kept is used by nobody: tokens given with it would name
                // nobody after the next start.
                Some(key) => match store.put(&table, KEY, &record(&key)) {
                    Ok(()) => (Some(key), None),
                    Err(why) => (None, Some(why.to_string())),
                },
                None => (None, Some("the system gave no random bytes".to_string())),
            },
            Err(_) => (None, None),
        };
        Signer {
            key: RwLock::new(key),
            unkept,
        }
    }

    /// Why the key made at the start could not be kept, where it could not: for the Host to say
    /// once in its log, as every write that fails is said.
    pub fn unkept(&self) -> Option<&str> {
        self.unkept.as_deref()
    }

    /// Whether the Host has its key: without it no turn is given a token, and a token presented
    /// is answered that it cannot be said whose it is.
    pub fn has_key(&self) -> bool {
        self.key().is_some()
    }

    /// The token of a turn of an agent; none without a key or an agent.
    pub fn token(&self, agent_id: &str, turn: u64) -> Option<String> {
        let key = self.key()?;
        (!agent_id.is_empty()).then(|| turn_identity::make(&key, agent_id, turn))
    }

    fn key(&self) -> Option<Key> {
        *self
            .key
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// The key, read again from the fleet's store where it could not be read at the start.
    async fn key_or_read(&self, fleet: &FleetHandle) -> Result<Key, Unavailable> {
        if let Some(key) = self.key() {
            return Ok(key);
        }
        let unavailable = || Unavailable(UNAVAILABLE.to_string());
        let kept = fleet.kept(KIND, KEY.to_string()).await;
        let key = kept.map_err(|_| unavailable())?.as_deref().and_then(key_of);
        let key = key.ok_or_else(unavailable)?;
        *self
            .key
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(key);
        Ok(key)
    }
}

/// The agent whose turn a presented token is: `None` for a token this Host did not make, of an
/// agent it does not have, or of a turn that neither runs nor ended in the last seven days. A key
/// or a log that cannot be read is no answer ([`UNAVAILABLE`]): it is not taken for "nobody".
pub async fn caller(app: &App, token: &str) -> Result<Option<Arc<Member>>, Unavailable> {
    let key = app.turns.key_or_read(&app.fleet).await?;
    let Some(claim) = turn_identity::read(&key, token) else {
        return Ok(None);
    };
    let directory = app.fleet.directory();
    let mut everyone = directory.members.iter().chain(directory.janitors.iter());
    let Some(member) = everyone.find(|member| member.record.agent_id == claim.agent_id) else {
        return Ok(None);
    };
    let standing = member.agent.standing(claim.turn).await;
    let standing = standing.map_err(|_| Unavailable(UNAVAILABLE.to_string()))?;
    let current = turn_identity::current(standing, app.clock.now_ms());
    Ok(current.then(|| member.clone()))
}

/// A new key from the system's source of randomness; none where it has none to give.
fn made() -> Option<Key> {
    let mut key = [0u8; KEY_BYTES];
    SystemRandom::new().fill(&mut key).ok().map(|()| key)
}

fn record(key: &Key) -> Vec<u8> {
    let key = key.iter().map(|byte| format!("{byte:02x}")).collect();
    serde_json::to_vec(&Kept { key }).unwrap_or_default()
}

/// The key a record keeps; none from a record of another form.
fn key_of(kept: &[u8]) -> Option<Key> {
    let kept: Kept = serde_json::from_slice(kept).ok()?;
    let text = kept.key.as_bytes();
    if text.len() != KEY_BYTES * 2 {
        return None;
    }
    let mut key = [0u8; KEY_BYTES];
    for (byte, pair) in key.iter_mut().zip(text.chunks(2)) {
        let pair = std::str::from_utf8(pair).ok()?;
        *byte = u8::from_str_radix(pair, 16).ok()?;
    }
    Some(key)
}

/// The agent whose turn a request is, by the token it presents (`None`: it presents none, and is
/// no turn's). A token that names nobody is refused (`verified turn identity required`, 403); one
/// whose agent cannot be said is answered so, and that it may be asked again (503).
pub async fn presented(app: &App, token: Option<&str>) -> Result<Option<Arc<Member>>, ApiError> {
    let Some(token) = token else {
        return Ok(None);
    };
    let found = caller(app, token).await;
    match found.map_err(|why| ApiError::retryable(StatusCode::SERVICE_UNAVAILABLE, why.0))? {
        Some(member) => Ok(Some(member)),
        None => {
            let why = "verified turn identity required";
            Err(ApiError::new(StatusCode::FORBIDDEN, why))
        }
    }
}
