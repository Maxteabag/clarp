//! The owner's settings, read anew each time the watcher looks.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

const SECOND: i64 = 1_000;
const MINUTE: i64 = 60 * SECOND;
const HOUR: i64 = 60 * MINUTE;

#[derive(Debug)]
pub(super) struct Settings {
    pub enabled: bool,
    pub interval: Duration,
    pub summary_hour: i8,
    pub front_desk: String,
    pub owners: BTreeMap<String, String>,
    pub wait_ms: i64,
    pub wait_count: usize,
    pub stuck_ms: i64,
    pub crash_events: usize,
    pub window_ms: i64,
    pub failed_sends: usize,
    pub card_age_ms: i64,
    pub release_hold_ms: i64,
    pub release_kinds: BTreeSet<String>,
}

impl Settings {
    pub(super) fn read() -> Self {
        let path = crate::config::host_config_dir().join("config.toml");
        let file = crate::owner::OwnerFile::new(Some(path));
        let table = file.read();
        let section = table
            .as_ref()
            .and_then(|table| table.get("experience_watch"));
        Self::from_section(section)
    }

    pub(super) fn from_section(section: Option<&toml::Value>) -> Self {
        let field = |key: &str| section.and_then(|section| section.get(key));
        let number = |value: Option<&toml::Value>, default| {
            value
                .and_then(toml::Value::as_integer)
                .filter(|n| *n >= 0)
                .unwrap_or(default)
        };
        let thresholds =
            |key: &str, default| number(field("thresholds").and_then(|t| t.get(key)), default);
        let count =
            |key: &str, default| usize::try_from(thresholds(key, default)).unwrap_or(usize::MAX);
        let front_desk = field("front_desk")
            .and_then(toml::Value::as_str)
            .unwrap_or("")
            .trim()
            .to_owned();
        let owners = field("owners")
            .and_then(toml::Value::as_table)
            .into_iter()
            .flatten()
            .filter_map(|(kind, session)| Some((kind.clone(), session.as_str()?.trim().to_owned())))
            .filter(|(_, session)| !session.is_empty())
            .collect();
        let release_kinds = field("release_kinds")
            .and_then(toml::Value::as_array)
            .map_or_else(
                || ["check", "test"].into_iter().map(str::to_owned).collect(),
                |items| {
                    items
                        .iter()
                        .filter_map(toml::Value::as_str)
                        .map(str::to_owned)
                        .collect()
                },
            );
        let interval_seconds = number(field("interval_seconds"), 300).max(30);
        Self {
            enabled: field("enabled").and_then(toml::Value::as_bool) == Some(true),
            interval: Duration::from_secs(u64::try_from(interval_seconds).unwrap_or(300)),
            summary_hour: i8::try_from(number(field("summary_hour"), 9))
                .ok()
                .filter(|n| *n < 24)
                .unwrap_or(9),
            front_desk,
            owners,
            wait_ms: thresholds("wait_seconds", 60).saturating_mul(SECOND),
            wait_count: count("wait_count", 1),
            stuck_ms: thresholds("queue_stuck_seconds", 600).saturating_mul(SECOND),
            crash_events: count("crash_events", 10),
            window_ms: thresholds("window_minutes", 15)
                .saturating_mul(MINUTE)
                .max(interval_seconds.saturating_mul(SECOND)),
            failed_sends: count("failed_sends", 1),
            card_age_ms: thresholds("card_age_hours", 24).saturating_mul(HOUR),
            release_hold_ms: thresholds("release_hold_minutes", 10).saturating_mul(MINUTE),
            release_kinds,
        }
    }

    pub(super) fn window_start(&self, now: i64, previous: i64) -> i64 {
        now.saturating_sub(self.window_ms).min(previous)
    }

    pub(super) fn recipients(&self, kind: &str) -> Vec<String> {
        let family = if kind.starts_with("app_crash_") {
            "app_crash"
        } else if kind.starts_with("scroll_flash_") {
            "scroll_flash"
        } else if matches!(kind, "wait" | "sends" | "stall") {
            "queue"
        } else {
            ""
        };
        // A scroll flash with no owner of its own goes to whoever owns that platform's app
        // crashes: the app's owner.
        let app_owner = kind.strip_prefix("scroll_flash_").and_then(|platform| {
            self.owners
                .get(&format!("app_crash_{platform}"))
                .or_else(|| self.owners.get("app_crash"))
        });
        let mut all = Vec::new();
        if let Some(owner) = self
            .owners
            .get(kind)
            .or_else(|| self.owners.get(family))
            .or(app_owner)
        {
            all.push(owner.clone());
        }
        if !self.front_desk.is_empty() && !all.contains(&self.front_desk) {
            all.push(self.front_desk.clone());
        }
        all
    }
}
