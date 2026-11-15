//! Whether the user sits at a desk with the app open.
//!
//! A desktop app that is in front of its user says so every few seconds, with a lease that
//! runs out by itself. While any such lease runs, a phone is not paged for a reply the user
//! can see on the screen before them. Nothing else depends on it: what is unread stays
//! unread, and nothing is kept back to be sent later.
//!
//! These are the Python Host's rules (`server/lib/desktop_presence.py`), word for word in
//! what they refuse.

use std::collections::BTreeMap;

use serde_json::Value;
use sha2::{Digest, Sha256};

/// How long a report keeps its desk present.
pub const LEASE_MS: i64 = 45_000;
/// How long a desk that went away is remembered, so that an old report cannot bring it back.
const TOMBSTONE_MS: i64 = 300_000;
const MAX_INSTANCES: usize = 256;
/// The largest whole number every client can count to.
const MAX_SEQUENCE: i64 = (1 << 53) - 1;

#[derive(Debug, Clone, PartialEq, Eq)]
struct Lease {
    owner: String,
    sequence: i64,
    expires_at: i64,
    updated_at: i64,
}

/// The desks that have said they are there.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Presence {
    /// By who reported and which instance of the app it was.
    leases: BTreeMap<String, Lease>,
}

/// A UUID as it is written in full, when the text is one in any of the usual forms.
fn uuid(given: &Value) -> Option<String> {
    let text = given.as_str()?;
    let text = text.strip_prefix("urn:uuid:").unwrap_or(text);
    let text = text.strip_prefix('{').unwrap_or(text);
    let text = text.strip_suffix('}').unwrap_or(text);
    let digits: String = text.chars().filter(|c| *c != '-').collect();
    if digits.len() != 32 || !digits.chars().all(|c| c.is_ascii_hexdigit()) {
        return None;
    }
    let digits = digits.to_ascii_lowercase();
    Some(format!(
        "{}-{}-{}-{}-{}",
        &digits[..8],
        &digits[8..12],
        &digits[12..16],
        &digits[16..20],
        &digits[20..]
    ))
}

/// A whole number that is no truth value and no fraction.
fn whole(given: &Value) -> Option<i64> {
    match given {
        Value::Number(number) if number.is_i64() || number.is_u64() => number.as_i64(),
        _ => None,
    }
}

impl Presence {
    pub fn new() -> Self {
        Self::default()
    }

    /// Takes a desk's report. `Ok(false)`: an older report than one already taken, which
    /// changes nothing. The error is what was wrong with the report.
    pub fn update(
        &mut self,
        principal: &str,
        instance_id: &Value,
        sequence: &Value,
        active: &Value,
        sent_at_ms: &Value,
        now: i64,
    ) -> Result<bool, String> {
        if principal.is_empty() {
            return Err("authenticated principal required".to_string());
        }
        let instance = uuid(instance_id).ok_or("instance_id must be a UUID")?;
        let sequence = whole(sequence)
            .filter(|sequence| (1..=MAX_SEQUENCE).contains(sequence))
            .ok_or("sequence must be a positive safe integer")?;
        let active = active.as_bool().ok_or("active must be boolean")?;
        let sent_at = whole(sent_at_ms)
            .filter(|sent_at| (now - LEASE_MS..=now + 5_000).contains(sent_at))
            .ok_or("presence report is stale or clock is out of sync")?;
        let key = Sha256::digest(format!("{principal}\0{instance}").as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        self.leases
            .retain(|_, lease| lease.updated_at >= now - TOMBSTONE_MS);
        match self.leases.get(&key) {
            Some(known) if sequence <= known.sequence => return Ok(false),
            None if self.leases.len() >= MAX_INSTANCES => {
                return Err("too many desktop instances".to_string());
            }
            _ => {}
        }
        let expires_at = if active {
            now.min(sent_at) + LEASE_MS
        } else {
            0
        };
        self.leases.insert(
            key,
            Lease {
                owner: principal.to_string(),
                sequence,
                expires_at,
                updated_at: now,
            },
        );
        Ok(true)
    }

    /// Whether a desk is there now. `may` says whether a reporter still may speak for the
    /// user: the administrator, or a device that is paired with full scope.
    pub fn active(&self, now: i64, may: &dyn Fn(&str) -> bool) -> bool {
        self.leases.values().any(|lease| {
            lease.updated_at > now - LEASE_MS
                && lease.updated_at <= now
                && lease.expires_at > now
                && may(&lease.owner)
        })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use serde_json::json;

    use super::*;

    const DESK: &str = "8f6f0d6a-3a9a-4f37-9d6b-0c2f8f3b7a11";

    fn report(
        presence: &mut Presence,
        sequence: i64,
        active: bool,
        now: i64,
    ) -> Result<bool, String> {
        presence.update(
            "administrator",
            &json!(DESK),
            &json!(sequence),
            &json!(active),
            &json!(now),
            now,
        )
    }

    #[test]
    fn a_desk_is_there_for_as_long_as_its_last_report_holds() {
        let mut presence = Presence::new();
        let anyone = |_: &str| true;
        assert!(!presence.active(1_000_000, &anyone));
        assert_eq!(report(&mut presence, 1, true, 1_000_000), Ok(true));
        assert!(presence.active(1_000_001, &anyone));
        assert!(presence.active(1_000_000 + LEASE_MS - 1, &anyone));
        assert!(
            !presence.active(1_000_000 + LEASE_MS, &anyone),
            "it ran out"
        );
        // An older report changes nothing; a newer one that says the desk is left ends it.
        assert_eq!(report(&mut presence, 1, false, 1_010_000), Ok(false));
        assert!(presence.active(1_010_001, &anyone));
        assert_eq!(report(&mut presence, 2, false, 1_010_000), Ok(true));
        assert!(!presence.active(1_010_001, &anyone));
        // A reporter that may no longer speak for the user is not a desk.
        assert_eq!(report(&mut presence, 3, true, 1_020_000), Ok(true));
        assert!(!presence.active(1_020_001, &|_| false));
    }

    #[test]
    fn a_report_that_cannot_be_trusted_is_refused_in_the_python_hosts_words() {
        let mut presence = Presence::new();
        let now = 1_000_000;
        let mut refused = |instance: Value, sequence: Value, active: Value, sent: Value| {
            presence
                .update("device_1", &instance, &sequence, &active, &sent, now)
                .unwrap_err()
        };
        assert_eq!(
            refused(json!("not one"), json!(1), json!(true), json!(now)),
            "instance_id must be a UUID"
        );
        assert_eq!(
            refused(json!(null), json!(1), json!(true), json!(now)),
            "instance_id must be a UUID"
        );
        for sequence in [json!(0), json!(-1), json!(1.5), json!(true), json!("1")] {
            assert_eq!(
                refused(json!(DESK), sequence, json!(true), json!(now)),
                "sequence must be a positive safe integer"
            );
        }
        assert_eq!(
            refused(json!(DESK), json!(1), json!(1), json!(now)),
            "active must be boolean"
        );
        for sent in [json!(now - LEASE_MS - 1), json!(now + 5_001), json!("now")] {
            assert_eq!(
                refused(json!(DESK), json!(1), json!(true), sent),
                "presence report is stale or clock is out of sync"
            );
        }
        assert_eq!(
            presence
                .update("", &json!(DESK), &json!(1), &json!(true), &json!(now), now)
                .unwrap_err(),
            "authenticated principal required"
        );
        // The forms a UUID is written in are one desk.
        let braced = json!(format!("{{{}}}", DESK.to_uppercase()));
        assert_eq!(
            presence.update(
                "device_1",
                &braced,
                &json!(1),
                &json!(true),
                &json!(now),
                now
            ),
            Ok(true)
        );
        assert_eq!(
            presence.update(
                "device_1",
                &json!(DESK.replace('-', "")),
                &json!(1),
                &json!(true),
                &json!(now),
                now
            ),
            Ok(false),
            "the same desk, and no newer report"
        );
    }
}
