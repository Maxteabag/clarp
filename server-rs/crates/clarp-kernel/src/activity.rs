//! Whether a person is at one of the apps: what an app reports of itself.
//!
//! An app that is open says so every little while: which running copy of it this is, how many
//! reports that copy has made, whether it is in front, and how long ago its user last touched
//! it. A report holds for three quarters of a minute. What the Host does by itself and would
//! rather not do while a person is there (an update, a restart) asks [`Leases::active`].
//!
//! The rules and their words are the Python Host's (`server/lib/application_activity.py`).
//! What is held is held in memory: a report is old after 45 seconds, which no restart
//! outlives usefully.

use std::collections::HashMap;

use serde_json::Value;

/// How long a report holds.
const HELD_MS: i64 = 45_000;
/// How far ahead of this Host's clock an app's clock may be.
const AHEAD_MS: i64 = 5_000;
/// After this long without a report, a copy of an app is forgotten.
const FORGOTTEN_MS: i64 = 300_000;
/// The most copies that are remembered.
const MOST: usize = 256;
/// The longest an input may be ago.
const DAY_MS: i64 = 86_400_000;

#[derive(Debug, Clone, PartialEq, Eq)]
struct Lease {
    owner: String,
    sequence: i64,
    foreground: bool,
    expires_at: i64,
    input_at: i64,
    reported_at: i64,
}

/// What the apps have reported of themselves.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Leases {
    by_copy: HashMap<(String, String), Lease>,
}

/// A whole number as JSON has one: not a truth value, not a fraction.
fn whole(value: Option<&Value>) -> Option<i64> {
    let number = value?.as_number()?;
    if number.is_f64() {
        return None;
    }
    number.as_i64()
}

/// The id of a running copy of an app, in the one form such an id has: thirty-two hex digits
/// in five groups. It is taken with or without its dashes, in braces, as a URN.
fn copy_id(given: Option<&Value>) -> Option<String> {
    let text = given?.as_str()?;
    let text = text.strip_prefix("urn:").unwrap_or(text);
    let text = text.strip_prefix("uuid:").unwrap_or(text);
    let text = text.trim_matches(|sign| sign == '{' || sign == '}');
    let hex: String = text.chars().filter(|sign| *sign != '-').collect();
    if hex.len() != 32 || !hex.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    let hex = hex.to_ascii_lowercase();
    Some(format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    ))
}

impl Leases {
    pub fn new() -> Self {
        Self::default()
    }

    /// Takes a report of an app that `principal` runs. `Ok(false)` for a report that is not
    /// newer than the last of the same copy; an error is what the app is told.
    pub fn report(&mut self, principal: &str, report: &Value, now: i64) -> Result<bool, String> {
        let Some(copy) = copy_id(report.get("instance_id")) else {
            return Err("instance_id must be a UUID".to_string());
        };
        let sequence =
            whole(report.get("sequence")).filter(|sequence| (1..1 << 53).contains(sequence));
        let (false, Some(sequence)) = (principal.is_empty(), sequence) else {
            return Err("invalid principal or sequence".to_string());
        };
        let foreground = report.get("foreground").and_then(Value::as_bool);
        let input_age = whole(report.get("input_age_ms")).filter(|age| (0..=DAY_MS).contains(age));
        let (Some(foreground), Some(input_age)) = (foreground, input_age) else {
            return Err("invalid foreground or input age".to_string());
        };
        let sent = whole(report.get("sent_at_ms"));
        let Some(sent) = sent.filter(|sent| (now - HELD_MS..=now + AHEAD_MS).contains(sent)) else {
            return Err("stale activity report".to_string());
        };
        self.by_copy
            .retain(|_, lease| lease.reported_at >= now - FORGOTTEN_MS);
        let key = (principal.to_string(), copy);
        match self.by_copy.get(&key) {
            Some(known) if sequence <= known.sequence => return Ok(false),
            None if self.by_copy.len() >= MOST => {
                return Err("too many activity instances".to_string());
            }
            _ => {}
        }
        let at = now.min(sent);
        let lease = Lease {
            owner: principal.to_string(),
            sequence,
            foreground,
            expires_at: at + HELD_MS,
            input_at: at - input_age,
            reported_at: now,
        };
        self.by_copy.insert(key, lease);
        Ok(true)
    }

    /// Whether a person is at an app right now: one that is in front, reported within the
    /// last three quarters of a minute, and touched within `idle_seconds`. `may` says
    /// whether whoever runs the app still may speak for a person (a device that was not
    /// taken away, with full scope).
    pub fn active(&self, idle_seconds: i64, now: i64, may: &dyn Fn(&str) -> bool) -> bool {
        self.by_copy.values().any(|lease| {
            lease.foreground
                && lease.reported_at > now - HELD_MS - 1
                && lease.expires_at > now
                && (0..idle_seconds.saturating_mul(1_000)).contains(&(now - lease.input_at))
                && may(&lease.owner)
        })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use serde_json::json;

    const NOW: i64 = 1_800_000_000_000;
    const COPY: &str = "0a1b2c3d-0000-4000-8000-0000000000aa";

    fn report(sequence: i64, foreground: bool, input_age: i64, sent: i64) -> Value {
        json!({ "instance_id": COPY, "sequence": sequence, "foreground": foreground,
                "input_age_ms": input_age, "sent_at_ms": sent })
    }

    #[test]
    fn an_app_in_front_is_a_person_at_it_for_three_quarters_of_a_minute() {
        let mut leases = Leases::new();
        let anyone = |_: &str| true;
        assert!(!leases.active(300, NOW, &anyone));
        assert_eq!(
            leases.report("administrator", &report(1, true, 2_000, NOW), NOW),
            Ok(true)
        );
        assert!(leases.active(300, NOW, &anyone));
        assert!(leases.active(300, NOW + 44_999, &anyone));
        assert!(
            !leases.active(300, NOW + 45_000, &anyone),
            "the report is old"
        );
        assert!(
            !leases.active(1, NOW, &anyone),
            "the last touch is longer ago than that"
        );
        assert!(
            !leases.active(300, NOW, &|_| false),
            "a device that was taken away"
        );
        // The same report again, and an older one, change nothing.
        assert_eq!(
            leases.report("administrator", &report(1, false, 0, NOW), NOW),
            Ok(false)
        );
        assert!(leases.active(300, NOW, &anyone));
        // Behind, it is no person at it.
        assert_eq!(
            leases.report("administrator", &report(2, false, 0, NOW + 10), NOW + 10),
            Ok(true)
        );
        assert!(!leases.active(300, NOW + 10, &anyone));
        // Another device counts for itself.
        assert_eq!(
            leases.report("device-1", &report(1, true, 0, NOW + 20), NOW + 20),
            Ok(true)
        );
        assert!(leases.active(300, NOW + 20, &anyone));
        assert!(!leases.active(300, NOW + 20, &|owner| owner != "device-1"));
    }

    #[test]
    fn a_report_that_is_none_is_refused_in_the_python_hosts_words() {
        let mut leases = Leases::new();
        let said =
            |leases: &mut Leases, report: Value| leases.report("administrator", &report, NOW);
        for no in [
            json!({}),
            json!({ "instance_id": 7 }),
            json!({ "instance_id": "not-an-id" }),
        ] {
            assert_eq!(
                said(&mut leases, no),
                Err("instance_id must be a UUID".to_string())
            );
        }
        let with = |change: Value| {
            let mut whole = report(1, true, 0, NOW);
            whole
                .as_object_mut()
                .unwrap()
                .extend(change.as_object().unwrap().clone());
            whole
        };
        for no in [
            json!({ "sequence": 0 }),
            json!({ "sequence": true }),
            json!({ "sequence": 1.0 }),
            json!({ "sequence": "1" }),
            json!({ "sequence": 9_007_199_254_740_992_i64 }),
        ] {
            assert_eq!(
                said(&mut leases, with(no)),
                Err("invalid principal or sequence".to_string())
            );
        }
        assert_eq!(
            leases.report("", &report(1, true, 0, NOW), NOW),
            Err("invalid principal or sequence".to_string())
        );
        for no in [
            json!({ "foreground": "yes" }),
            json!({ "foreground": 1 }),
            json!({ "input_age_ms": -1 }),
            json!({ "input_age_ms": 86_400_001 }),
            json!({ "input_age_ms": "now" }),
        ] {
            assert_eq!(
                said(&mut leases, with(no)),
                Err("invalid foreground or input age".to_string())
            );
        }
        for no in [
            json!({ "sent_at_ms": 1 }),
            json!({ "sent_at_ms": NOW - 45_001 }),
            json!({ "sent_at_ms": NOW + 5_001 }),
            json!({ "sent_at_ms": "now" }),
        ] {
            assert_eq!(
                said(&mut leases, with(no)),
                Err("stale activity report".to_string())
            );
        }
        // An id is an id however it is written.
        for written in [
            "0A1B2C3D-0000-4000-8000-0000000000AA",
            "0a1b2c3d000040008000 0000000000aa",
        ] {
            let taken = said(
                &mut leases,
                with(json!({ "instance_id": written, "sequence": 9 })),
            );
            assert_eq!(taken.is_ok(), !written.contains(' '), "{written}");
        }
        assert_eq!(
            said(
                &mut leases,
                with(json!({ "instance_id": format!("{{{COPY}}}"), "sequence": 9 }))
            ),
            Ok(false)
        );
    }

    #[test]
    fn copies_that_stopped_reporting_are_forgotten_and_there_are_not_too_many() {
        let mut leases = Leases::new();
        for number in 0..MOST {
            let copy = format!("00000000-0000-4000-8000-{number:012x}");
            let report = json!({ "instance_id": copy, "sequence": 1, "foreground": true,
                                 "input_age_ms": 0, "sent_at_ms": NOW });
            assert_eq!(leases.report("administrator", &report, NOW), Ok(true));
        }
        let one_more = json!({ "instance_id": "ffffffff-0000-4000-8000-000000000000", "sequence": 1,
                               "foreground": true, "input_age_ms": 0, "sent_at_ms": NOW });
        assert_eq!(
            leases.report("administrator", &one_more, NOW),
            Err("too many activity instances".to_string())
        );
        // A copy that is known still reports.
        let known = json!({ "instance_id": "00000000-0000-4000-8000-000000000000", "sequence": 2,
                            "foreground": false, "input_age_ms": 0, "sent_at_ms": NOW });
        assert_eq!(leases.report("administrator", &known, NOW), Ok(true));
        // Five minutes on, the old ones are gone.
        let later = NOW + FORGOTTEN_MS + 1;
        let later_report = json!({ "instance_id": "ffffffff-0000-4000-8000-000000000000", "sequence": 1,
                                   "foreground": true, "input_age_ms": 0, "sent_at_ms": later });
        assert_eq!(
            leases.report("administrator", &later_report, later),
            Ok(true)
        );
    }
}
