//! Stored findings and the messages owed for their transitions.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

use super::settings::Settings;

const DAY: i64 = 24 * 60 * 60 * 1_000;
const MAX_CLEARED: usize = 2_000;
const DETAIL_CHARS: usize = 2_000;

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
    pub first_seen: i64,
    pub last_seen: i64,
    pub count: u64,
    pub detail: String,
    pub cleared_at: Option<i64>,
    pub notified_open: Vec<String>,
    pub notified_clear: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub(super) enum Notice {
    Open(u64),
    Clear(u64),
    Summary(String),
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

fn local(ms: i64) -> Option<jiff::Zoned> {
    let ts = jiff::Timestamp::from_millisecond(ms).ok()?;
    Some(ts.to_zoned(jiff::tz::TimeZone::system()))
}

impl Book {
    pub fn reconcile(&mut self, mut seen: Seen, settings: &Settings, now: i64) {
        for row in self.rows.iter_mut().filter(|row| row.cleared_at.is_none()) {
            let key = (row.kind.clone(), row.subject.clone());
            if let Some(detail) = seen.found.remove(&key) {
                row.last_seen = now;
                row.count = row.count.saturating_add(1);
                row.detail = detail.chars().take(DETAIL_CHARS).collect();
            } else if !seen.unavailable.contains(&key)
                && !seen.unavailable_kinds.contains(row.kind.as_str())
            {
                row.cleared_at = Some(now);
                let since = local(row.first_seen)
                    .map(|time| time.strftime("%H:%M").to_string())
                    .unwrap_or_default();
                let text = format!(
                    "Experience watch: {} cleared for {} (open since {}, seen {}x).",
                    row.kind, row.subject, since, row.count,
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
            let detail: String = detail.chars().take(DETAIL_CHARS).collect();
            let text = format!(
                "Experience watch: {kind} opened for {subject}. {detail} (clarp-admin experience --open)"
            );
            for to in settings.recipients(&kind) {
                self.owed.push(Owed {
                    notice: Notice::Open(self.next),
                    to,
                    text: text.clone(),
                });
            }
            self.rows.push(Row {
                id: self.next,
                kind,
                subject,
                first_seen: now,
                last_seen: now,
                count: 1,
                detail,
                cleared_at: None,
                notified_open: Vec::new(),
                notified_clear: Vec::new(),
            });
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
        self.daily(settings, now);
    }

    fn daily(&mut self, settings: &Settings, now: i64) {
        let Some(time) = local(now) else { return };
        let day = time.strftime("%Y-%m-%d").to_string();
        self.owed.retain(|owed| {
            !matches!(&owed.notice, Notice::Summary(date) if date != &day || settings.front_desk.is_empty())
        });
        if settings.front_desk.is_empty() {
            return;
        }
        if time.hour() < settings.summary_hour
            || self.summary_date == day
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
                "Experience watch, last 24 h: {opened} opened, {cleared} cleared, {open} still open{detail}. clarp-admin experience --since 24h"
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
