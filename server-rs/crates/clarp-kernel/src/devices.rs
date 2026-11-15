//! The register of the devices a Host tells of finished turns: who gets a push.
//!
//! A phone registers the token its push service gave it. The token is how the phone is
//! reached, so the register's rules are about who may say where a token points: a token
//! belongs to the paired device that registered it and is never taken over by another, a new
//! token of the same phone retires its old one, and a registration that proves nothing (no
//! paired device behind it) binds nothing and overwrites nothing that is bound.
//!
//! These are the Python Host's rules (`server/lib/apns.py`: `register_token`,
//! `disable_tokens` and the small writes beside them), row for row. Nothing here sends
//! anything: a change is answered as the rows it leaves, for the Host to keep.

use std::collections::BTreeMap;
use std::net::IpAddr;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

/// One token and where it points. Named as the Python Host's table names its columns.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PushRow {
    pub token: String,
    /// The agent the device had open when it registered, when it said.
    pub session: Option<String>,
    pub platform: String,
    /// The push service's side the token belongs to: `production` or `sandbox`.
    pub environment: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub last_push_at: Option<i64>,
    /// Since when nothing is sent to it. Registered again by its owner, it lives again.
    pub disabled_at: Option<i64>,
    /// Where the device reaches this Host, for what a push links to.
    pub base_url: String,
    /// The phone's own leave to push to it through the relay. Empty: none.
    pub push_grant: String,
    /// The paired device that owns the token. Empty: nobody proved to.
    pub principal: String,
    /// The push service: `apns` or `fcm`.
    pub provider: String,
    /// What an Android phone said it can run, as a JSON list. Empty: nothing.
    pub supported_conversation_modes: String,
}

/// A registration as it was asked for. An empty text is "not said".
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Registration<'a> {
    pub token: &'a str,
    pub session: &'a str,
    pub environment: &'a str,
    pub platform: &'a str,
    pub base_url: &'a str,
    pub push_grant: &'a str,
    /// The paired device that asks, with full scope and not revoked. Empty: someone else.
    pub owner: &'a str,
    pub provider: &'a str,
    pub modes: &'a [String],
}

/// Why a change of the register was refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Refused {
    /// Nothing that could be done was asked for.
    Value(String),
    /// The token is not one its push service gives out, or belongs to the other service.
    Invalid(String),
    /// The token belongs to another paired device. Nothing was written.
    OwnedElsewhere,
}

/// The register.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct PushBook {
    rows: BTreeMap<String, PushRow>,
}

fn is_hex(text: &str) -> bool {
    !text.is_empty() && text.chars().all(|c| c.is_ascii_hexdigit())
}

/// One name for one token: hex digits in lower case (the push service takes both cases for
/// the same token), anything else as it is, trimmed.
pub fn canonical_token(token: &str) -> String {
    let token = token.trim();
    if is_hex(token) {
        token.to_ascii_lowercase()
    } else {
        token.to_string()
    }
}

/// A phone-issued leave to push, when it is well formed: `pg1.` and three runs of hex digits.
pub fn push_grant(raw: &str) -> &str {
    let value = raw.trim();
    let hex = |part: &str, len: usize| {
        part.len() == len
            && part
                .chars()
                .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
    };
    let parts: Vec<&str> = value.split('.').collect();
    match parts.as_slice() {
        ["pg1", a, b, c] if hex(a, 64) && hex(b, 16) && hex(c, 64) => value,
        _ => "",
    }
}

/// The push service of a registration: the one it names, else its platform's. Empty when it
/// names one nobody knows.
pub fn provider_for(platform: &str, provider: &str) -> &'static str {
    match provider.trim().to_ascii_lowercase().as_str() {
        "" => {
            let platform = platform.trim().to_ascii_lowercase();
            if platform == "android" { "fcm" } else { "apns" }
        }
        "apns" => "apns",
        "fcm" => "fcm",
        _ => "",
    }
}

/// Whether plain HTTP may be spoken to this host: an address on a private network, a
/// link-local one or one of an overlay network (100.64.0.0/10), never the machine itself;
/// or a name of the local network.
fn reachable_without_tls(host: &str) -> bool {
    match host.parse::<IpAddr>() {
        Ok(IpAddr::V4(address)) => {
            let [a, b, c, _] = address.octets();
            let private = matches!(
                (a, b, c),
                (0 | 10, _, _)
                    | (172, 16..=31, _)
                    | (192, 168, _)
                    | (192, 0, 0 | 2)
                    | (198, 18 | 19, _)
                    | (198, 51, 100)
                    | (203, 0, 113)
                    | (240..=255, _, _)
            );
            let overlay = a == 100 && (64..=127).contains(&b);
            (private || address.is_link_local() || overlay) && !address.is_loopback()
        }
        Ok(IpAddr::V6(address)) => {
            let first = address.segments()[0];
            let unique_local = first & 0xfe00 == 0xfc00;
            let link_local = first & 0xffc0 == 0xfe80;
            (unique_local || link_local) && !address.is_loopback()
        }
        Err(_) => host.ends_with(".local"),
    }
}

/// The address a device gave for this Host, when it is one to keep: HTTPS, or plain HTTP to
/// a private or overlay address or a `.local` name; never with credentials, a query or a
/// fragment. Without a trailing slash.
pub fn device_base_url(raw: &str) -> String {
    let value = raw.trim();
    let Some((scheme, rest)) = value.split_once("://") else {
        return String::new();
    };
    let scheme = scheme.to_ascii_lowercase();
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let (netloc, tail) = rest.split_at(end);
    let path_end = tail.find(['?', '#']).unwrap_or(tail.len());
    let (path, extra) = tail.split_at(path_end);
    if !extra.trim_matches(['?', '#']).is_empty() {
        return String::new();
    }
    let (credentials, address) = match netloc.rsplit_once('@') {
        Some((credentials, address)) => (credentials, address),
        None => ("", netloc),
    };
    if !credentials.trim_matches(':').is_empty() {
        return String::new();
    }
    let host = match address.strip_prefix('[') {
        Some(bracketed) => bracketed.split(']').next().unwrap_or(""),
        None => address.split(':').next().unwrap_or(""),
    }
    .to_ascii_lowercase();
    let kept = match scheme.as_str() {
        "https" => !host.is_empty(),
        "http" => !host.is_empty() && reachable_without_tls(&host),
        _ => false,
    };
    if !kept {
        return String::new();
    }
    format!("{scheme}://{netloc}{}", path.trim_end_matches('/'))
}

/// A token as FCM gives them out: URL-safe base64 and colons, 20 to 4096 characters.
fn fcm_token(token: &str) -> bool {
    (20..=4096).contains(&token.len())
        && token
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "_:-".contains(c))
}

/// A token as APNs gives them to a device: 64 to 200 hex digits.
fn apns_token(token: &str) -> bool {
    (64..=200).contains(&token.len())
        && token
            .chars()
            .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c))
}

impl PushBook {
    pub fn new() -> Self {
        Self::default()
    }

    /// Every row, by token.
    pub fn rows(&self) -> impl Iterator<Item = &PushRow> {
        self.rows.values()
    }

    /// Takes rows as a change left them.
    pub fn apply(&mut self, changed: &[PushRow]) {
        for row in changed {
            self.rows.insert(row.token.clone(), row.clone());
        }
    }

    /// The rows of `work` that are not as the register has them.
    fn changed(&self, work: BTreeMap<String, PushRow>) -> Vec<PushRow> {
        work.into_values()
            .filter(|row| self.rows.get(&row.token) != Some(row))
            .collect()
    }

    /// Registers a token, or registers it again: the rows this leaves changed. `can_receive`
    /// says whether a paired device still is one (full scope, not revoked).
    pub fn register(
        &self,
        given: &Registration<'_>,
        can_receive: &dyn Fn(&str) -> bool,
        now: i64,
    ) -> Result<Vec<PushRow>, Refused> {
        let invalid = |why: String| Err(Refused::Invalid(why));
        let token = canonical_token(given.token);
        if token.is_empty() {
            return Err(Refused::Value("empty device token".to_string()));
        }
        let environment = match given.environment.trim().to_lowercase() {
            said if said.is_empty() => "production".to_string(),
            said => said,
        };
        let session = Some(given.session.trim().to_string()).filter(|said| !said.is_empty());
        let platform = match given.platform.trim() {
            "" => "ios",
            said => said,
        };
        let provider = provider_for(platform, given.provider);
        if provider.is_empty() {
            return invalid(format!("unknown push provider '{}'", given.provider));
        }
        let modes = if provider == "fcm" && platform == "android" && !given.modes.is_empty() {
            let mut once: Vec<&String> = Vec::new();
            for mode in given.modes {
                if !once.contains(&mode) {
                    once.push(mode);
                }
            }
            json!(once).to_string()
        } else {
            String::new()
        };
        if provider == "fcm" && !fcm_token(&token) {
            return invalid("not an FCM registration token".to_string());
        }
        // Only a registration from a phone's own app binds a token to its device. Any other
        // (a desktop, a browser) binds nothing, and is never refused for the token's form.
        let owner = match platform {
            "ios" | "android" => given.owner.trim(),
            _ => "",
        };
        if !owner.is_empty() && provider == "apns" && !apns_token(&token) {
            return invalid("device token must be 64-200 hex digits".to_string());
        }

        let mut work = self.rows.clone();
        // A token of hex digits is one token in either case: every stored spelling counts.
        let hex = is_hex(&token);
        let same = |row: &PushRow| {
            if hex {
                row.token.to_ascii_lowercase() == token
            } else {
                row.token == token
            }
        };
        if !owner.is_empty() {
            // A token whose owner is no longer a paired device passes to the device that
            // registers it now: the same phone, paired again. Only to a device.
            for row in work.values_mut().filter(|row| same(row)) {
                let stale = !row.principal.is_empty()
                    && row.principal != owner
                    && !can_receive(&row.principal);
                if stale {
                    row.principal = owner.to_string();
                }
            }
        }
        if work
            .values()
            .any(|row| same(row) && !row.principal.is_empty() && row.principal != owner)
        {
            return Err(Refused::OwnedElsewhere);
        }
        // A token belongs to one push service for good.
        if work.values().any(|row| {
            let theirs = if row.provider.is_empty() {
                "apns"
            } else {
                row.provider.as_str()
            };
            same(row) && theirs != provider
        }) {
            return invalid(format!(
                "token is registered for another push provider, not {provider}"
            ));
        }
        let grant = push_grant(given.push_grant);
        for row in work.values_mut().filter(|row| row.disabled_at.is_none()) {
            let other = row.token != token;
            let retired = if owner.is_empty() {
                // Nobody proved anything: only rows nobody owns are touched. The same session
                // on the same platform had another token before; a grant follows the phone,
                // so the row that had it would reach the same phone a second time.
                let unbound = row.principal.is_empty();
                let same_seat = session
                    .as_deref()
                    .is_some_and(|session| row.session.as_deref() == Some(session))
                    && row.platform == platform;
                let same_phone = !grant.is_empty() && row.push_grant == grant;
                other && unbound && (same_seat || same_phone)
            } else {
                // The device's own other spellings of this token, and its older token for
                // the same service: a rotated token replaces it.
                let mine = row.principal == owner;
                let spelling = hex && row.token.to_ascii_lowercase() == token;
                let older = row.platform == platform
                    && row.environment == environment
                    && row.provider == provider;
                other && mine && (spelling || older)
            };
            if retired {
                row.disabled_at = Some(now);
            }
        }
        let base_url = device_base_url(given.base_url);
        match work.get_mut(&token) {
            Some(row) => {
                row.session = session;
                row.platform = platform.to_string();
                row.provider = provider.to_string();
                row.supported_conversation_modes = modes;
                row.environment = environment;
                // What was not sent again stays as it was.
                if !base_url.is_empty() {
                    row.base_url = base_url;
                }
                if !grant.is_empty() {
                    row.push_grant = grant.to_string();
                }
                if row.principal.is_empty() {
                    row.principal = owner.to_string();
                }
                row.updated_at = now;
                row.disabled_at = None;
            }
            None => {
                work.insert(
                    token.clone(),
                    PushRow {
                        token,
                        session,
                        platform: platform.to_string(),
                        environment,
                        created_at: now,
                        updated_at: now,
                        last_push_at: None,
                        disabled_at: None,
                        base_url,
                        push_grant: grant.to_string(),
                        principal: owner.to_string(),
                        provider: provider.to_string(),
                        supported_conversation_modes: modes,
                    },
                );
            }
        }
        Ok(self.changed(work))
    }

    /// Stops pushes to live rows: by token (every spelling of one of hex digits), by owner,
    /// or both; `only_owner` keeps a token to that owner's rows (a device unregistering its
    /// own). The rows this leaves changed: none when nothing matched.
    pub fn disable(
        &self,
        token: &str,
        owner: &str,
        only_owner: Option<&str>,
        now: i64,
    ) -> Result<Vec<PushRow>, Refused> {
        if token.is_empty() && owner.is_empty() && only_owner.is_none() {
            return Err(Refused::Value(
                "a token or an owner is required".to_string(),
            ));
        }
        let wanted = canonical_token(token);
        let hex = is_hex(&wanted);
        let mut work = self.rows.clone();
        for row in work.values_mut().filter(|row| row.disabled_at.is_none()) {
            let by_token = token.is_empty()
                || if hex {
                    row.token.to_ascii_lowercase() == wanted
                } else {
                    row.token == wanted
                };
            let by_owner = owner.is_empty() || row.principal == owner;
            let by_only = only_owner.is_none_or(|only| row.principal == only);
            if by_token && by_owner && by_only {
                row.disabled_at = Some(now);
            }
        }
        Ok(self.changed(work))
    }

    /// The push service said the token is dead.
    pub fn dead(&self, token: &str, now: i64) -> Option<PushRow> {
        let mut row = self.rows.get(token)?.clone();
        row.disabled_at = Some(now);
        Some(row)
    }

    /// The phone took its leave to push back. The row may still be reached another way.
    pub fn grant_revoked(&self, token: &str) -> Option<PushRow> {
        let mut row = self.rows.get(token)?.clone();
        row.push_grant = String::new();
        Some(row)
    }

    /// A push went out to the token.
    pub fn pushed(&self, token: &str, now: i64) -> Option<PushRow> {
        let mut row = self.rows.get(token)?.clone();
        row.last_push_at = Some(now);
        Some(row)
    }

    /// The rows something is sent to.
    pub fn active(&self) -> impl Iterator<Item = &PushRow> {
        self.rows.values().filter(|row| row.disabled_at.is_none())
    }

    /// The rows as they are shown, the last changed first, without the whole token: all of
    /// them, or one device's.
    pub fn shown(&self, owner: Option<&str>, can_receive: &dyn Fn(&str) -> bool) -> Vec<Value> {
        let mut rows: Vec<&PushRow> = self
            .rows
            .values()
            .filter(|row| owner.is_none_or(|owner| row.principal == owner))
            .collect();
        rows.sort_by_key(|row| std::cmp::Reverse(row.updated_at));
        rows.into_iter()
            .map(|row| {
                json!({
                    "platform": row.platform,
                    "provider": row.provider,
                    "environment": row.environment,
                    "owner": row.principal,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                    "last_push_at": row.last_push_at,
                    "disabled_at": row.disabled_at,
                    "token_prefix": row.token.chars().take(12).collect::<String>(),
                    "owner_can_receive": row.principal.is_empty() || can_receive(&row.principal),
                })
            })
            .collect()
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn an_address_is_kept_only_when_it_is_safe_to_follow() {
        assert_eq!(
            device_base_url(" https://host.example:7682/ "),
            "https://host.example:7682"
        );
        assert_eq!(
            device_base_url("http://100.80.1.2:7682"),
            "http://100.80.1.2:7682",
            "an overlay network's address"
        );
        assert_eq!(
            device_base_url("http://192.168.1.20:7682/clarp/"),
            "http://192.168.1.20:7682/clarp"
        );
        assert_eq!(
            device_base_url("http://printer.local:7682"),
            "http://printer.local:7682"
        );
        for refused in [
            "http://8.8.8.8:7682",
            "http://127.0.0.1:7682",
            "http://example.com",
            "https://user:secret@host.example/",
            "https://host.example/?token=1",
            "https://host.example/#part",
            "ftp://host.example/",
            "host.example",
            "",
        ] {
            assert_eq!(device_base_url(refused), "", "{refused}");
        }
    }

    #[test]
    fn a_grant_is_one_only_in_its_own_form() {
        let grant = format!(
            "pg1.{}.{}.{}",
            "1".repeat(64),
            "2".repeat(16),
            "3".repeat(64)
        );
        assert_eq!(push_grant(&format!("  {grant} ")), grant);
        assert_eq!(push_grant("pg1.short"), "");
        assert_eq!(push_grant(&grant.to_uppercase()), "");
        assert_eq!(push_grant(""), "");
    }
}
