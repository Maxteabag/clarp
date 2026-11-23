//! Stored findings and the messages owed for their transitions.

use std::collections::{BTreeMap, BTreeSet};

use jiff::tz::TimeZone;
use serde::{Deserialize, Serialize};

use super::{clock, local, settings::Settings};
use crate::fleet::Directory;

const DAY: i64 = 24 * 60 * 60 * 1_000;
const MAX_CLEARED: usize = 2_000;
const DETAIL_CHARS: usize = 2_000;
const HOUR: i64 = 60 * 60 * 1_000;

/// A finding that tells of what changed while it is open, at most hourly: the apps' scroll
/// flashes (one per device, while new flashes come).
fn retold(kind: &str) -> bool {
    kind.starts_with("scroll_flash_")
}

pub(super) type Key = (String, String);

#[derive(Default)]
pub(super) struct Seen {
    pub found: BTreeMap<Key, String>,
    pub unavailable: BTreeSet<Key>,
    pub unavailable_kinds: BTreeSet<&'static str>,
}

impl Seen {
    pub fn add(&mut self, kind: &str, subject: &str, detail: String) {
        self.found
            .insert((kind.to_owned(), subject.to_owned()), detail);
    }

    pub fn missing(&mut self, kind: &str, subject: &str) {
        self.unavailable
            .insert((kind.to_owned(), subject.to_owned()));
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(super) struct Row {
    pub id: u64,
    pub kind: String,
    pub subject: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    device_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    agent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    agent_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    agent_session: Option<String>,
    pub first_seen: i64,
    pub last_seen: i64,
    pub count: u64,
    pub detail: String,
    pub cleared_at: Option<i64>,
    pub notified_open: Vec<String>,
    pub notified_clear: Vec<String>,
    /// When its recipients were last told of it, for a finding told again while open.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_told: Option<i64>,
    /// It changed since they were last told.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub untold: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(super) enum Notice {
    Open(u64),
    Clear(u64),
    Summary(String),
    /// What changed of an open finding, at the moment it was told.
    Update(u64, i64),
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(super) struct Owed {
    pub notice: Notice,
    pub to: String,
    pub text: String,
}

impl Owed {
    pub fn message_id(&self) -> String {
        let step = match &self.notice {
            Notice::Open(id) => format!("open-{id}"),
            Notice::Clear(id) => format!("clear-{id}"),
            Notice::Summary(day) => format!("daily-{day}"),
            Notice::Update(id, at) => format!("update-{id}-{at}"),
        };
        format!("experience-watch-{step}-{}", self.to)
    }
}

#[derive(Default, Serialize, Deserialize)]
pub(super) struct Book {
    pub rows: Vec<Row>,
    pub owed: Vec<Owed>,
    next: u64,
    summary_date: String,
}

pub(super) fn agent_label(name: &str, session: &str) -> String {
    if name.is_empty() || name == session {
        session.to_owned()
    } else {
        format!("{name} ({session})")
    }
}

impl Row {
    pub fn refresh_subject(&mut self, directory: &Directory) {
        if self.kind.starts_with("app_crash_") || retold(&self.kind) {
            if let Some(device) = directory
                .devices
                .values()
                .find(|device| device.device_id == self.subject && !device.name.trim().is_empty())
            {
                self.device_name = Some(device.name.clone());
            }
            return;
        }

        if self.agent_id.is_none() {
            self.agent_id = match self.kind.as_str() {
                "wait" | "queue" | "sends" | "stall" => Some(self.subject.clone()),
                "release" => directory
                    .members
                    .iter()
                    .chain(&directory.janitors)
                    .find(|member| {
                        member
                            .agent
                            .view()
                            .jobs
                            .iter()
                            .any(|job| job.id == self.subject)
                    })
                    .map(|member| member.record.agent_id.clone()),
                _ => None,
            };
        }
        if let Some((name, session)) = self
            .agent_id
            .as_deref()
            .and_then(|id| directory.shown_as(id))
        {
            self.agent_name = Some(name);
            self.agent_session = Some(session);
        }
    }

    pub fn subject_label(&self) -> String {
        if let Some(session) = self.agent_session.as_deref() {
            let agent = agent_label(self.agent_name.as_deref().unwrap_or_default(), session);
            return if self.kind == "release" {
                format!("{} ({agent})", self.subject)
            } else {
                agent
            };
        }
        self.device_name.as_ref().map_or_else(
            || self.subject.clone(),
            |name| format!("{name} ({})", self.subject),
        )
    }
}

pub(super) fn summary_day(settings: &Settings, zone: &TimeZone, now: i64) -> Option<String> {
    if settings.front_desk.is_empty() {
        return None;
    }
    let time = local(now, zone)?;
    (time.hour() == settings.summary_hour).then(|| time.strftime("%Y-%m-%d").to_string())
}

impl Book {
    pub fn reconcile(
        &mut self,
        mut seen: Seen,
        settings: &Settings,
        directory: &Directory,
        zone: &TimeZone,
        now: i64,
    ) {
        for row in &mut self.rows {
            row.refresh_subject(directory);
            if row.cleared_at.is_some() {
                continue;
            }
            let key = (row.kind.clone(), row.subject.clone());
            if let Some(detail) = seen.found.remove(&key) {
                row.last_seen = now;
                row.count = row.count.saturating_add(1);
                let detail: String = detail.chars().take(DETAIL_CHARS).collect();
                if retold(&row.kind) {
                    row.untold |= detail != row.detail;
                    let last = row.last_told.unwrap_or(row.first_seen);
                    if row.untold && now.saturating_sub(last) >= HOUR {
                        row.untold = false;
                        row.last_told = Some(now);
                        let text = format!(
                            "Experience watch: {} still open for {} at {}. {} (clarp-admin experience --open)",
                            row.kind,
                            row.subject_label(),
                            clock(now, zone),
                            detail,
                        );
                        for to in settings.recipients(&row.kind) {
                            self.owed.push(Owed {
                                notice: Notice::Update(row.id, now),
                                to,
                                text: text.clone(),
                            });
                        }
                    }
                }
                row.detail = detail;
            } else if !seen.unavailable.contains(&key)
                && !seen.unavailable_kinds.contains(row.kind.as_str())
            {
                row.cleared_at = Some(now);
                let text = format!(
                    "Experience watch: {} cleared for {} at {} (open since {}, seen {}x).",
                    row.kind,
                    row.subject_label(),
                    clock(now, zone),
                    clock(row.first_seen, zone),
                    row.count,
                );
                for to in settings.recipients(&row.kind) {
                    self.owed.push(Owed {
                        notice: Notice::Clear(row.id),
                        to,
                        text: text.clone(),
                    });
                }
            }
        }
        for ((kind, subject), detail) in seen.found {
            self.next = self.next.saturating_add(1);
            let mut row = Row {
                id: self.next,
                kind,
                subject,
                device_name: None,
                agent_id: None,
                agent_name: None,
                agent_session: None,
                first_seen: now,
                last_seen: now,
                count: 1,
                detail: detail.chars().take(DETAIL_CHARS).collect(),
                cleared_at: None,
                notified_open: Vec::new(),
                notified_clear: Vec::new(),
                last_told: Some(now),
                untold: false,
            };
            row.refresh_subject(directory);
            let text = format!(
                "Experience watch: {} opened for {} at {}. {} (clarp-admin experience --open)",
                row.kind,
                row.subject_label(),
                clock(now, zone),
                row.detail,
            );
            for to in settings.recipients(&row.kind) {
                self.owed.push(Owed {
                    notice: Notice::Open(row.id),
                    to,
                    text: text.clone(),
                });
            }
            self.rows.push(row);
        }
        let mut kept = 0;
        self.rows
            .sort_unstable_by_key(|row| std::cmp::Reverse((row.cleared_at, row.id)));
        self.rows.retain(|row| {
            if let Some(cleared) = row.cleared_at {
                kept += 1;
                cleared >= now.saturating_sub(7 * DAY) && kept <= MAX_CLEARED
            } else {
                true
            }
        });
        self.daily(settings, zone, now);
    }

    fn daily(&mut self, settings: &Settings, zone: &TimeZone, now: i64) {
        let day = summary_day(settings, zone, now);
        self.owed.retain(
            |owed| !matches!(&owed.notice, Notice::Summary(date) if day.as_ref() != Some(date)),
        );
        let Some(day) = day else { return };
        if self.summary_date == day
            || self
                .owed
                .iter()
                .any(|owed| matches!(&owed.notice, Notice::Summary(date) if date == &day))
        {
            return;
        }
        let since = now.saturating_sub(DAY);
        let opened = self
            .rows
            .iter()
            .filter(|row| row.first_seen >= since)
            .count();
        let cleared = self
            .rows
            .iter()
            .filter(|row| row.cleared_at.is_some_and(|at| at >= since))
            .count();
        let mut kinds: BTreeMap<&str, usize> = BTreeMap::new();
        for row in self.rows.iter().filter(|row| row.cleared_at.is_none()) {
            *kinds.entry(&row.kind).or_default() += 1;
        }
        let open = kinds.values().sum::<usize>();
        let kinds = kinds
            .into_iter()
            .map(|(kind, count)| format!("{kind} {count}"))
            .collect::<Vec<_>>()
            .join(", ");
        let detail = if kinds.is_empty() {
            String::new()
        } else {
            format!(" ({kinds})")
        };
        self.owed.push(Owed {
            notice: Notice::Summary(day),
            to: settings.front_desk.clone(),
            text: format!(
                "Experience watch, last 24 h through {}: {opened} opened, {cleared} cleared, {open} still open{detail}. clarp-admin experience --since 24h",
                clock(now, zone),
            ),
        });
    }

    pub fn told(&mut self, owed: Owed) {
        let (id, clearing) = match owed.notice {
            Notice::Summary(day) => {
                self.summary_date = day;
                return;
            }
            Notice::Open(id) => (id, false),
            Notice::Clear(id) => (id, true),
            Notice::Update(..) => return,
        };
        if let Some(row) = self.rows.iter_mut().find(|row| row.id == id) {
            let recipients = if clearing {
                &mut row.notified_clear
            } else {
                &mut row.notified_open
            };
            if !recipients.contains(&owed.to) {
                recipients.push(owed.to);
            }
        }
    }
}
