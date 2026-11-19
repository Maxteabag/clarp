//! Who is calling: the identity the Host gives an agent's turn.
//!
//! Every turn's tool process is given a token in its environment ([`TURN_ENV`]). A Clarp tool
//! inside the turn presents it (in the header `X-Clarp-Turn`, or with the call of a tool), and so
//! does a worker the turn left behind; the Host then knows which agent acts, instead of a session
//! name the caller types.
//!
//! How a token is made is this Host's own. The Python Host makes a random one and keeps which agent
//! it gave it to in a table (`provider_background_jobs.identity_env`, `provider_turns`). This Host
//! keeps nothing per turn: a token says whose turn it is (a version, the agent's id, the turn's
//! number) and is signed with a key of the Host's own, an HMAC-SHA256 over exactly what it says
//! ([`make`], [`read`]). Whether the turn it names still counts is asked of the agent's own log
//! ([`Standing`], [`current`]), with the Python Host's rule: while the turn runs, and for seven days
//! after it ended (`TURN_RETENTION_MS`, after which that Host forgets an ended turn's token). What
//! comes of a token is held to that Host's `lib.turn_identity.caller_agent_id` by
//! `tests/turn_identity_vectors.rs`.
//!
//! The limit is the Python Host's too: agents share one Unix user, so a process that reads another
//! process's environment can take its token.

use hmac::{Hmac, Mac};
use sha2::Sha256;

/// The environment variable a turn's token is given in.
pub const TURN_ENV: &str = "CLARP_PROVIDER_TURN";
/// How long a token names its agent after its turn ended: a worker a turn left behind keeps it, as
/// the Python Host keeps an ended turn's for seven days (`TURN_RETENTION_MS`).
pub const KEPT_MS: i64 = 7 * 86_400_000;
/// How many bytes the Host's key is.
pub const KEY_BYTES: usize = 32;
/// The way a token is made, its first field. A token of another version names nobody.
pub const VERSION: &str = "1";
/// What separates a token's fields: a version, the agent's id in hex, the turn's number, the MAC.
const SEPARATOR: char = '.';
/// How many hex signs a MAC is.
const MAC_SIGNS: usize = 64;

/// What a token that verifies says: whose turn it is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Claim {
    pub agent_id: String,
    pub turn: u64,
}

/// Where the turn a token names stands, as the agent's log says it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Standing {
    /// The turn runs.
    Live,
    /// The turn is over: when it ended.
    Ended { at_ms: i64 },
    /// The log knows no such turn, or it has not begun.
    Unknown,
}

/// The token of one turn of an agent, signed with the Host's key.
pub fn make(key: &[u8], agent_id: &str, turn: u64) -> String {
    let said = said(agent_id, turn);
    let mac = hex(&signed(key, &said).finalize().into_bytes());
    format!("{said}{SEPARATOR}{mac}")
}

/// What a presented token says, where it is one this key made: nobody for a token of another
/// form or version, or whose MAC does not verify over every field it says. The MAC is checked
/// in constant time.
pub fn read(key: &[u8], token: &str) -> Option<Claim> {
    let (said, mac) = token.rsplit_once(SEPARATOR)?;
    let mut fields = said.split(SEPARATOR);
    let (version, agent, turn) = (fields.next()?, fields.next()?, fields.next()?);
    if fields.next().is_some() || version != VERSION || mac.len() != MAC_SIGNS {
        return None;
    }
    let agent_id = String::from_utf8(unhex(agent)?)
        .ok()
        .filter(|id| !id.is_empty())?;
    let turn = number(turn)?;
    let given = unhex(mac)?;
    signed(key, said).verify_slice(&given).ok()?;
    Some(Claim { agent_id, turn })
}

/// Whether a turn that stands so still names its agent: while it runs, and for [`KEPT_MS`] after
/// it ended, the last moment included. A turn the log does not know names nobody.
pub fn current(standing: Standing, now_ms: i64) -> bool {
    match standing {
        Standing::Live => true,
        Standing::Ended { at_ms } => now_ms <= at_ms.saturating_add(KEPT_MS),
        Standing::Unknown => false,
    }
}

/// What a token says before its MAC: its version, the agent's id in hex and the turn's number.
fn said(agent_id: &str, turn: u64) -> String {
    let agent = hex(agent_id.as_bytes());
    format!("{VERSION}{SEPARATOR}{agent}{SEPARATOR}{turn}")
}

/// The MAC over what a token says, every field of it, to be finished or checked.
fn signed(key: &[u8], said: &str) -> Hmac<Sha256> {
    // An HMAC takes a key of any length: this cannot fail.
    let signer = <Hmac<Sha256> as Mac>::new_from_slice(key).unwrap_or_else(|_| unreachable!());
    signer.chain_update(said.as_bytes())
}

/// A turn's number as a token writes it: decimal signs, the first not 0.
fn number(text: &str) -> Option<u64> {
    let digits = !text.is_empty() && text.bytes().all(|sign| sign.is_ascii_digit());
    (digits && !text.starts_with('0')).then(|| text.parse().ok())?
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Bytes from lowercase hex; nothing from a text of another form.
fn unhex(text: &str) -> Option<Vec<u8>> {
    if text.is_empty() || !text.len().is_multiple_of(2) {
        return None;
    }
    let value = |sign: u8| match sign {
        b'0'..=b'9' => Some(sign - b'0'),
        b'a'..=b'f' => Some(sign - b'a' + 10),
        _ => None,
    };
    let pairs = text.as_bytes().chunks(2);
    pairs
        .map(|pair| Some((value(pair[0])? << 4) | value(pair[1])?))
        .collect()
}
