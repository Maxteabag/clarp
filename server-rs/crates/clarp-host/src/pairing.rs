//! Pairing: how a device comes to be trusted by a Host.
//!
//! The administrator has the Host issue a code. The code is shown once, used once and soon
//! expires. A device hands it in and gets a token of its own, which is what it presents from
//! then on and what the administrator can revoke. The Host keeps neither the code nor the
//! token, only their digests: whoever reads the Host's files cannot speak as a device.
//!
//! This is the Python Host's scheme, word for word on the wire (`clp_` codes, `cld_` tokens,
//! `POST /pairing/exchange`), so an app pairs with either Host the same way.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::ids;

pub const DEFAULT_TTL_SECONDS: i64 = 10 * 60;
pub const MAX_TTL_SECONDS: i64 = 60 * 60;
const MIN_TTL_SECONDS: i64 = 30;
const CODE_PREFIX: &str = "clp_";
const TOKEN_PREFIX: &str = "cld_";
/// Neither a code nor a token is ever shorter than this, prefix included.
const MIN_SECRET_LEN: usize = 30;

/// What a paired device may do.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Scope {
    /// Everything the administrator may do.
    Full,
    /// Read conversations and send to agents; nothing that changes the Host or its devices.
    Limited,
}

impl Scope {
    pub fn parse(text: &str) -> Option<Self> {
        match text.trim().to_ascii_lowercase().as_str() {
            "" | "full" => Some(Self::Full),
            "limited" => Some(Self::Limited),
            _ => None,
        }
    }
}

/// A code that waits to be handed in, as the Host keeps it: under the digest of the code.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PendingCode {
    pub device_name: String,
    pub scope: Scope,
    pub created_at: i64,
    pub expires_at: i64,
}

/// A code as the administrator is shown it, once.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Issued {
    pub code: String,
    pub device_name: String,
    pub scope: Scope,
    pub created_at: i64,
    pub expires_at: i64,
}

/// A paired device as the Host keeps it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Device {
    pub device_id: String,
    pub name: String,
    /// The digest of the device's token. The token itself is not kept.
    #[serde(skip_serializing_if = "String::is_empty", default)]
    pub token_hash: String,
    pub scope: Scope,
    pub created_at: i64,
    pub last_seen_at: i64,
    pub revoked_at: Option<i64>,
}

impl Device {
    /// The device as anyone but the Host's own store may see it.
    pub fn public(&self) -> Self {
        Self {
            token_hash: String::new(),
            ..self.clone()
        }
    }
}

/// What a device is given when it hands in a code: itself, and its token, once.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Paired {
    pub device_id: String,
    pub device_name: String,
    pub scope: Scope,
    pub token: String,
    pub created_at: i64,
}

/// The digest a code or a token is kept and looked up under.
pub fn digest(secret: &str) -> String {
    Sha256::digest(secret.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// A name as the Host keeps it: single spaces, at most 80 characters, never empty.
fn tidy(name: &str) -> String {
    let tidy: String = name.split_whitespace().collect::<Vec<_>>().join(" ");
    let tidy: String = tidy.chars().take(80).collect();
    if tidy.is_empty() {
        "iPhone".to_string()
    } else {
        tidy
    }
}

/// Makes a code. Returns what the administrator is shown and what the Host keeps.
pub fn issue(
    device_name: &str,
    scope: Scope,
    ttl_seconds: i64,
    now_ms: i64,
) -> Result<(Issued, PendingCode), String> {
    if !(MIN_TTL_SECONDS..=MAX_TTL_SECONDS).contains(&ttl_seconds) {
        return Err("pairing expiry must be between 30 and 3600 seconds".to_string());
    }
    let pending = PendingCode {
        device_name: tidy(device_name),
        scope,
        created_at: now_ms,
        expires_at: now_ms + ttl_seconds * 1_000,
    };
    let issued = Issued {
        code: format!("{CODE_PREFIX}{}", ids::secret(32)),
        device_name: pending.device_name.clone(),
        scope,
        created_at: pending.created_at,
        expires_at: pending.expires_at,
    };
    Ok((issued, pending))
}

/// Whether a text can be a code at all. One that cannot is refused before any lookup.
pub fn looks_like_a_code(code: &str) -> bool {
    code.starts_with(CODE_PREFIX) && code.len() >= MIN_SECRET_LEN
}

/// Whether a text can be a device's token at all.
pub fn looks_like_a_token(token: &str) -> bool {
    token.starts_with(TOKEN_PREFIX) && token.len() >= MIN_SECRET_LEN
}

/// Turns a code that was handed in into a device. Returns what the device is given and what
/// the Host keeps. The caller has looked the code up by its digest and removes it afterwards.
pub fn redeem(
    pending: &PendingCode,
    device_name: &str,
    now_ms: i64,
) -> Result<(Paired, Device), String> {
    if pending.expires_at < now_ms {
        return Err("pairing code has expired".to_string());
    }
    let named = if device_name.trim().is_empty() {
        pending.device_name.as_str()
    } else {
        device_name
    };
    let token = format!("{TOKEN_PREFIX}{}", ids::secret(36));
    let device = Device {
        device_id: format!("device_{}", ids::hex(16)),
        name: tidy(named),
        token_hash: digest(&token),
        scope: pending.scope,
        created_at: now_ms,
        last_seen_at: now_ms,
        revoked_at: None,
    };
    let paired = Paired {
        device_id: device.device_id.clone(),
        device_name: device.name.clone(),
        scope: device.scope,
        token,
        created_at: now_ms,
    };
    Ok((paired, device))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_code_becomes_a_device_with_a_token_the_host_does_not_keep() {
        let (issued, pending) = issue("  Peter's   iPhone ", Scope::Limited, 600, 1_000).unwrap();
        assert!(looks_like_a_code(&issued.code) && issued.code.starts_with("clp_"));
        assert_eq!(issued.device_name, "Peter's iPhone");
        assert_eq!(issued.expires_at, 601_000);
        assert_eq!(
            (pending.scope, pending.expires_at),
            (Scope::Limited, 601_000)
        );

        let (paired, device) = redeem(&pending, "", 2_000).unwrap();
        assert!(looks_like_a_token(&paired.token) && paired.token.starts_with("cld_"));
        assert_eq!(paired.device_name, "Peter's iPhone");
        assert_eq!(
            (paired.scope, device.scope),
            (Scope::Limited, Scope::Limited)
        );
        assert!(paired.device_id.starts_with("device_") && paired.device_id == device.device_id);
        assert_eq!(device.token_hash, digest(&paired.token));
        assert_ne!(
            device.token_hash, paired.token,
            "the token itself is not kept"
        );
        assert_eq!(
            device.public().token_hash,
            "",
            "and its digest is not shown"
        );

        let (renamed, _) = redeem(&pending, "Desk phone", 2_000).unwrap();
        assert_eq!(
            renamed.device_name, "Desk phone",
            "a device may name itself"
        );
    }

    #[test]
    fn a_code_expires_and_an_expiry_has_limits() {
        let (_, pending) = issue("iPhone", Scope::Full, 30, 0).unwrap();
        assert!(redeem(&pending, "", 30_000).is_ok());
        assert_eq!(
            redeem(&pending, "", 30_001).unwrap_err(),
            "pairing code has expired"
        );
        assert!(issue("iPhone", Scope::Full, 29, 0).is_err());
        assert!(issue("iPhone", Scope::Full, 3_601, 0).is_err());
        assert_eq!(
            issue("", Scope::Full, 60, 0).unwrap().0.device_name,
            "iPhone"
        );
    }

    #[test]
    fn what_cannot_be_a_secret_is_told_apart_without_a_lookup() {
        assert!(!looks_like_a_code("clp_short"));
        assert!(!looks_like_a_code(&format!("cld_{}", "x".repeat(40))));
        assert!(!looks_like_a_token(&format!("clp_{}", "x".repeat(40))));
        assert!(looks_like_a_token(&format!("cld_{}", "x".repeat(40))));
        assert_eq!(Scope::parse("Limited"), Some(Scope::Limited));
        assert_eq!(Scope::parse(""), Some(Scope::Full));
        assert_eq!(Scope::parse("root"), None);
        // The digest is SHA-256, as the Python Host's is: a device paired there reads the same.
        assert_eq!(
            digest("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
}
