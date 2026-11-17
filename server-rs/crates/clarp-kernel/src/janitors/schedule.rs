//! A janitor's schedule: a cron expression read in a time zone.
//!
//! `30 8 * * 1-5` in `Europe/Oslo` is half past eight on that city's clocks, whatever the
//! season. The rule is the Python Host's (`server/lib/janitor_schedule.py`): the local days
//! are gone through one by one; a local time that does not come (the hour a clock is put
//! forward over) is not run, and one that comes twice (the hour it is put back over) is run
//! the first time it comes. The zones are the machine's own table, which is what the Python
//! Host reads through `zoneinfo`.
//!
//! Held to that module by `tests/fixtures/janitor-schedule-vectors.json`
//! (`server-rs/tools/janitor-schedule-vectors.py`): the next run after each of three
//! thousand moments around the days clocks change, in zones with rules of every kind.

use jiff::Timestamp;
use jiff::civil::Date;
use jiff::tz::{AmbiguousOffset, TimeZone};

use crate::schedules::{self, Cron};

/// How many days ahead a next run is looked for: a year and a day, and one more.
const AHEAD_DAYS: usize = 367;
/// What a schedule that names no second run is taken to be apart: a day.
const A_DAY_S: i64 = 86_400;
/// The most runs a preview shows.
const PREVIEW_MOST: i64 = 10;

/// Why a schedule cannot be read, in the Python Host's words.
pub const NO_ZONE: &str = "Choose a valid IANA timezone";

/// The time zone of this name, when the machine has one. A name is the zone's own, letter
/// for letter: the table's files are named so, and the Python Host finds a zone by its file.
pub fn zone(name: &str) -> Option<TimeZone> {
    let found = TimeZone::get(name).ok()?;
    (found.iana_name() == Some(name)).then_some(found)
}

/// The first moment after `from_ms` that the expression names on the zone's clocks, in
/// milliseconds; `None` when there is none within a year and two days.
pub fn next_in(cron: &Cron, zone: &TimeZone, from_ms: i64) -> Option<i64> {
    let from = Timestamp::from_millisecond(from_ms).ok()?;
    let mut date = Date::from(zone.to_datetime(from));
    for _ in 0..AHEAD_DAYS {
        let weekday = i64::from(date.weekday().to_sunday_zero_offset());
        let named = cron.months.contains(&i64::from(date.month()))
            && cron.days.contains(&i64::from(date.day()))
            && cron.weekdays.contains(&weekday);
        if named {
            for hour in &cron.hours {
                for minute in &cron.minutes {
                    let (Ok(hour), Ok(minute)) = (i8::try_from(*hour), i8::try_from(*minute))
                    else {
                        continue;
                    };
                    let on_the_clock = zone.to_ambiguous_timestamp(date.at(hour, minute, 0, 0));
                    // A time the clock is put forward over does not come.
                    if matches!(on_the_clock.offset(), AmbiguousOffset::Gap { .. }) {
                        continue;
                    }
                    // One the clock is put back over comes twice: the first time is it.
                    let Ok(at) = on_the_clock.earlier() else {
                        continue;
                    };
                    if at.as_millisecond() > from_ms {
                        return Some(at.as_millisecond());
                    }
                }
            }
        }
        date = date.tomorrow().ok()?;
    }
    None
}

/// When a schedule runs next after `from_ms`. `Err`: the expression is none, or the zone
/// is none, in the Python Host's words.
pub fn next_run(expression: &str, zone_name: &str, from_ms: i64) -> Result<Option<i64>, String> {
    let cron = schedules::parse(expression)?;
    let zone = zone(zone_name).ok_or_else(|| NO_ZONE.to_string())?;
    Ok(next_in(&cron, &zone, from_ms))
}

/// The time between a schedule's next two runs, in seconds: at least a minute, and a day
/// where it has no two runs. `Err` as for [`next_run`].
pub fn base_interval(expression: &str, zone_name: &str, from_ms: i64) -> Result<i64, String> {
    let cron = schedules::parse(expression)?;
    let zone = zone(zone_name).ok_or_else(|| NO_ZONE.to_string())?;
    let first = next_in(&cron, &zone, from_ms);
    let second = first.and_then(|first| next_in(&cron, &zone, first));
    Ok(match (first, second) {
        (Some(first), Some(second)) => ((second - first) / 1000).max(60),
        _ => A_DAY_S,
    })
}

/// The next runs of a schedule after `from_ms`, as an app previews them: at most ten, and
/// none where none is asked for. `Err` as for [`next_run`], when one is looked for at all.
pub fn preview(
    expression: &str,
    zone_name: &str,
    from_ms: i64,
    count: i64,
) -> Result<Vec<i64>, String> {
    let mut runs = Vec::new();
    let mut from_ms = from_ms;
    for _ in 0..count.clamp(0, PREVIEW_MOST) {
        match next_run(expression, zone_name, from_ms)? {
            Some(at) => {
                runs.push(at);
                from_ms = at;
            }
            None => break,
        }
    }
    Ok(runs)
}
