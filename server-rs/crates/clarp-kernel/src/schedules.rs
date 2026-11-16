//! Schedules: a prompt an agent is sent at times a cron expression names.
//!
//! The owner or an agent makes a schedule for an agent: a name, five fields of cron
//! (minute, hour, day of the month, month, day of the week; in UTC) or one of the short
//! names for them, and what to send. It can be turned off and on, changed and removed. A
//! runner looks every few seconds for what is due, moves each schedule on to its next time
//! first, so that none fires twice, and then sends its prompt.
//!
//! This is the Python Host's scheduler (`server/lib/scheduler.py`) as rules without a
//! store or a clock: which expressions are taken and why not, in its words; when an
//! expression runs next (within a year and a day, or never); and what making, changing,
//! listing and running answer.
//!
//! Held to the Python Host's own module by `tests/fixtures/schedule-vectors.json`.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};

use crate::python;

const MINUTE_S: i64 = 60;
const HOUR_S: i64 = 3_600;
const DAY_S: i64 = 86_400;
/// How far ahead a next time is looked for.
const AHEAD_S: i64 = 366 * DAY_S;

/// The short names for an expression.
fn shortcut(expression: &str) -> Option<&'static str> {
    Some(match expression {
        "@hourly" => "0 * * * *",
        "@daily" | "@midnight" => "0 0 * * *",
        "@weekly" => "0 0 * * 0",
        "@monthly" => "0 0 1 * *",
        _ => return None,
    })
}

/// A whole number as a cron field writes one, or why it is none, in Python's words.
fn number(text: &str) -> Result<i128, String> {
    python::int(text).ok_or_else(|| {
        format!(
            "invalid literal for int() with base 10: {}",
            python::text(text)
        )
    })
}

/// The values one field allows: a list of single values, ranges, `*`, and steps over any of
/// them. A single value outside the field's bounds is refused; a range or a step that
/// reaches outside is cut to them.
fn field(text: &str, least: i128, most: i128) -> Result<BTreeSet<i64>, String> {
    let mut allowed = BTreeSet::new();
    // Every `step`-th value from `from` to `to`, as far as the field has them: the same
    // values as counting through the whole range would give, without counting through it.
    let mut stepped = |from: i128, to: i128, step: i128| {
        let short = least.saturating_sub(from);
        let first = if short <= 0 {
            Some(from)
        } else {
            // The first of the steps that is in the field.
            let steps = short.saturating_add(step - 1) / step;
            steps.checked_mul(step).and_then(|up| from.checked_add(up))
        };
        let mut next = first;
        while let Some(value) = next.filter(|value| *value <= to.min(most)) {
            allowed.insert(value as i64);
            next = value.checked_add(step);
        }
    };
    for part in python::strip(text).split(',') {
        let part = python::strip(part);
        if part.is_empty() {
            continue;
        }
        if let Some((range, step)) = part.split_once('/') {
            let step = number(step)?;
            if step <= 0 {
                return Err(format!("Step must be > 0 in {part}"));
            }
            let (from, to) = match range.split_once('-') {
                _ if range == "*" => (least, most),
                Some((from, to)) => (number(from)?, number(to)?),
                None => (number(range)?, most),
            };
            stepped(from, to, step);
        } else if let Some((from, to)) = part.split_once('-') {
            stepped(number(from)?, number(to)?, 1);
        } else if part == "*" {
            stepped(least, most, 1);
        } else {
            let value = number(part)?;
            if !(least..=most).contains(&value) {
                return Err(format!("Value {value} out of range ({least}-{most})"));
            }
            stepped(value, value, 1);
        }
    }
    if allowed.is_empty() {
        return Err(format!("Empty or invalid field expression: {text}"));
    }
    Ok(allowed)
}

/// An expression, read: the minutes, hours, days of the month, months and days of the week
/// (Sunday is 0) it allows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Cron {
    pub minutes: BTreeSet<i64>,
    pub hours: BTreeSet<i64>,
    pub days: BTreeSet<i64>,
    pub months: BTreeSet<i64>,
    pub weekdays: BTreeSet<i64>,
}

/// Reads an expression: five fields, or a short name. `Err`: why it is none, in the Python
/// Host's words.
pub fn parse(expression: &str) -> Result<Cron, String> {
    let stripped = python::strip(expression);
    let fields: Vec<&str> = shortcut(stripped)
        .unwrap_or(stripped)
        .split(python::blank)
        .filter(|part| !part.is_empty())
        .collect();
    let [minutes, hours, days, months, weekdays] = fields.as_slice() else {
        return Err(format!(
            "Cron expression must have 5 fields, got {}: {expression}",
            fields.len()
        ));
    };
    Ok(Cron {
        minutes: field(minutes, 0, 59)?,
        hours: field(hours, 0, 23)?,
        days: field(days, 1, 31)?,
        months: field(months, 1, 12)?,
        // Sunday is 0, and 7 as well.
        weekdays: field(weekdays, 0, 7)?
            .into_iter()
            .map(|day| day % 7)
            .collect(),
    })
}

/// A day counted from 1970-01-01 as its year, month and day.
fn date(days: i64) -> (i64, i64, i64) {
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let of_era = shifted.rem_euclid(146_097);
    let year_of_era = (of_era - of_era / 1_460 + of_era / 36_524 - of_era / 146_096) / 365;
    let of_year = of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let shifted_month = (5 * of_year + 2) / 153;
    let day = of_year - (153 * shifted_month + 2) / 5 + 1;
    let month = if shifted_month < 10 {
        shifted_month + 3
    } else {
        shifted_month - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

fn days_in(year: i64, month: i64) -> i64 {
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

impl Cron {
    /// The first minute after `from_ms` the expression names, in UTC, as milliseconds; `None`
    /// when there is none within a year and a day.
    pub fn next_run(&self, from_ms: i64) -> Option<i64> {
        // From the beginning of the next minute.
        let mut at = from_ms.div_euclid(60_000) * MINUTE_S + MINUTE_S;
        let until = at + AHEAD_S;
        while at < until {
            let (days, of_day) = (at.div_euclid(DAY_S), at.rem_euclid(DAY_S));
            let (hour, minute) = (of_day / HOUR_S, of_day % HOUR_S / MINUTE_S);
            let (year, month, day) = date(days);
            if !self.months.contains(&month) {
                // On to the first day of the next month.
                at += (days_in(year, month) - day + 1) * DAY_S - of_day;
                continue;
            }
            // 1970-01-01 was a Thursday.
            let weekday = (days + 4).rem_euclid(7);
            if !self.days.contains(&day) || !self.weekdays.contains(&weekday) {
                at += DAY_S - of_day;
                continue;
            }
            if !self.hours.contains(&hour) {
                at += HOUR_S - minute * MINUTE_S;
                continue;
            }
            if self.minutes.contains(&minute) {
                return Some(at * 1000);
            }
            at += MINUTE_S;
        }
        None
    }
}

/// When an expression runs next after `from_ms`. `Err`: it is no expression.
pub fn next_run(expression: &str, from_ms: i64) -> Result<Option<i64>, String> {
    Ok(parse(expression)?.next_run(from_ms))
}

/// One schedule, as it is kept and as an app reads it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Schedule {
    pub schedule_id: String,
    pub agent_id: String,
    /// The agent's session when the schedule was made.
    pub session: String,
    pub name: String,
    pub cron_expression: String,
    pub prompt: String,
    pub enabled: bool,
    pub last_run_at: Option<i64>,
    pub next_run_at: Option<i64>,
    pub created_at: i64,
    pub updated_at: i64,
}

/// What a schedule is made with.
#[derive(Debug, Clone, Copy)]
pub struct Made<'a> {
    /// The agent it is for, as the Host found it by what the request named: its id and its
    /// session. `None`: the Host has no such agent.
    pub agent: Option<(&'a str, &'a str)>,
    /// What the request named the agent by.
    pub named: &'a str,
    pub name: &'a str,
    pub cron: &'a str,
    pub prompt: &'a str,
    pub enabled: bool,
}

/// What of a schedule is changed: only what is given.
#[derive(Debug, Clone, Copy, Default)]
pub struct Change<'a> {
    pub enabled: Option<bool>,
    pub name: Option<&'a str>,
    pub cron: Option<&'a str>,
    pub prompt: Option<&'a str>,
}

/// The schedules of a Host, in the order they were made.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Schedules {
    rows: Vec<Schedule>,
}

impl Schedules {
    /// From what is kept.
    pub fn kept(rows: Vec<Schedule>) -> Self {
        Self { rows }
    }

    /// Every schedule, to be kept.
    pub fn rows(&self) -> &[Schedule] {
        &self.rows
    }

    pub fn get(&self, schedule_id: &str) -> Option<&Schedule> {
        self.rows
            .iter()
            .find(|schedule| schedule.schedule_id == schedule_id)
    }

    /// A schedule is made. `new_id` is a new id of hex signs: the first twelve name it.
    /// `Err`: why not, in the Python Host's words.
    pub fn create(&mut self, made: &Made, new_id: &str, now_ms: i64) -> Result<&Schedule, String> {
        let Some((agent_id, session)) = made.agent else {
            return Err(format!(
                "Agent session {} not found",
                python::text(made.named)
            ));
        };
        let cron = parse(made.cron)?;
        let id: String = new_id.chars().take(12).collect();
        self.rows.push(Schedule {
            schedule_id: format!("sched_{id}"),
            agent_id: agent_id.to_string(),
            session: session.to_string(),
            name: python::strip(made.name).to_string(),
            cron_expression: python::strip(made.cron).to_string(),
            prompt: python::strip(made.prompt).to_string(),
            enabled: made.enabled,
            last_run_at: None,
            next_run_at: made.enabled.then(|| cron.next_run(now_ms)).flatten(),
            created_at: now_ms,
            updated_at: now_ms,
        });
        self.rows.last().ok_or_else(String::new)
    }

    /// A schedule is changed. `Ok(None)`: there is none of this id. Turned on, or given
    /// another expression while on, its next time is reckoned from now; turned off it has
    /// none; otherwise the time it had stands.
    pub fn update(
        &mut self,
        schedule_id: &str,
        change: &Change,
        now_ms: i64,
    ) -> Result<Option<&Schedule>, String> {
        let Some(place) = self
            .rows
            .iter()
            .position(|schedule| schedule.schedule_id == schedule_id)
        else {
            return Ok(None);
        };
        let was = &self.rows[place];
        let expression = change.cron.map(python::strip);
        let cron = match expression {
            Some(expression) => Some(parse(expression)?),
            None => None,
        };
        let enabled = change.enabled.unwrap_or(was.enabled);
        let next_run_at = if enabled && (!was.enabled || cron.is_some()) {
            let read = match cron {
                Some(cron) => cron,
                None => parse(&was.cron_expression)?,
            };
            read.next_run(now_ms)
        } else if enabled {
            was.next_run_at
        } else {
            None
        };
        let schedule = &mut self.rows[place];
        if let Some(name) = change.name {
            schedule.name = python::strip(name).to_string();
        }
        if let Some(expression) = expression {
            schedule.cron_expression = expression.to_string();
        }
        if let Some(prompt) = change.prompt {
            schedule.prompt = python::strip(prompt).to_string();
        }
        schedule.enabled = enabled;
        schedule.next_run_at = next_run_at;
        schedule.updated_at = now_ms;
        Ok(Some(&self.rows[place]))
    }

    /// A schedule is removed. Answers whether there was one.
    pub fn delete(&mut self, schedule_id: &str) -> bool {
        let before = self.rows.len();
        self.rows
            .retain(|schedule| schedule.schedule_id != schedule_id);
        self.rows.len() < before
    }

    /// The schedules of one agent by its id, else of one session, in the order they were
    /// made; or all of them by session.
    pub fn list(&self, session: &str, agent_id: &str) -> Vec<&Schedule> {
        let all = agent_id.is_empty() && session.is_empty();
        let rows = self.rows.iter();
        let mut listed: Vec<&Schedule> = rows
            .filter(|schedule| match (agent_id, session) {
                ("", "") => true,
                ("", session) => schedule.session == session,
                (agent_id, _) => schedule.agent_id == agent_id,
            })
            .collect();
        listed.sort_by_key(|schedule| schedule.created_at);
        if all {
            listed.sort_by(|a, b| a.session.cmp(&b.session));
        }
        listed
    }

    /// What is due now: on, and with a time that has come; the one longest due first.
    pub fn due(&self, now_ms: i64) -> Vec<&Schedule> {
        let mut due: Vec<&Schedule> = self
            .rows
            .iter()
            .filter(|schedule| {
                schedule.enabled && schedule.next_run_at.is_some_and(|next| next <= now_ms)
            })
            .collect();
        due.sort_by_key(|schedule| schedule.next_run_at);
        due
    }

    /// The runner's turn: every schedule that is due is moved on to its next time, and its
    /// agent and prompt are handed back to be sent: `(agent_id, session, prompt)`, the one
    /// longest due first.
    pub fn run(&mut self, now_ms: i64) -> Vec<(String, String, String)> {
        let due: Vec<String> = self
            .due(now_ms)
            .into_iter()
            .map(|schedule| schedule.schedule_id.clone())
            .collect();
        let mut sent = Vec::new();
        for schedule_id in due {
            let Some(schedule) = self
                .rows
                .iter_mut()
                .find(|schedule| schedule.schedule_id == schedule_id)
            else {
                continue;
            };
            // Moved on first: whatever becomes of the sending, it does not fire twice.
            schedule.last_run_at = Some(now_ms);
            let cron = parse(&schedule.cron_expression).ok();
            schedule.next_run_at = cron.and_then(|cron| cron.next_run(now_ms));
            schedule.updated_at = now_ms;
            sent.push((
                schedule.agent_id.clone(),
                schedule.session.clone(),
                schedule.prompt.clone(),
            ));
        }
        sent
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_day_is_the_date_the_calendar_gives_it() {
        assert_eq!(date(0), (1970, 1, 1));
        assert_eq!(date(59), (1970, 3, 1));
        assert_eq!(date(11_016), (2000, 2, 29));
        assert_eq!(date(11_017), (2000, 3, 1));
        assert_eq!(date(20_773), (2026, 11, 16));
        assert_eq!(date(21_243), (2028, 2, 29));
        assert_eq!(date(-1), (1969, 12, 31));
        assert_eq!(
            (
                days_in(2028, 2),
                days_in(2026, 2),
                days_in(2100, 2),
                days_in(2000, 2)
            ),
            (29, 28, 28, 29)
        );
    }

    #[test]
    fn a_range_that_reaches_far_outside_its_field_is_cut_to_it_without_being_counted_through() {
        // Where the Python Host counts through the whole range, value by value.
        let cron = parse("0-999999999999999999/7 * * * *").unwrap();
        assert_eq!(
            cron.minutes.iter().copied().collect::<Vec<_>>(),
            [0, 7, 14, 21, 28, 35, 42, 49, 56]
        );
        let cron = parse("-999999999999-5 * * * *");
        assert!(cron.is_err(), "a range cannot begin with a sign");
        let late = parse("* * * * 3/2").unwrap();
        assert_eq!(late.weekdays.iter().copied().collect::<Vec<_>>(), [0, 3, 5]);
    }
}
