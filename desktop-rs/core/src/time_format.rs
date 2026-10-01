//! Chat-list stamps, transcript day headings and running durations. Port of
//! the C++ client's `TimeFormat`. `now` is injected so tests are deterministic;
//! instants are shown in `now`'s time zone. Text follows the en_US QLocale
//! formats the C++ client produced on this machine (short time "6:10 PM",
//! short date "6/12/26").

use chrono::{DateTime, Datelike, Days, FixedOffset, NaiveDate, NaiveDateTime, TimeZone};

fn short_time<Tz: TimeZone>(moment: &DateTime<Tz>) -> String
where
    Tz::Offset: std::fmt::Display,
{
    moment.format("%-I:%M %p").to_string()
}

fn short_date(day: NaiveDate) -> String {
    day.format("%-m/%-d/%y").to_string()
}

/// ISO-8601 with or without milliseconds; an instant without an offset is
/// read in `zone` (Qt reads it as local time).
fn parse_iso<Tz: TimeZone>(timestamp: &str, zone: &Tz) -> Option<DateTime<Tz>> {
    if timestamp.is_empty() {
        return None;
    }
    if let Ok(parsed) = DateTime::<FixedOffset>::parse_from_rfc3339(timestamp) {
        return Some(parsed.with_timezone(zone));
    }
    ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M"]
        .iter()
        .find_map(|format| NaiveDateTime::parse_from_str(timestamp, format).ok())
        .and_then(|naive| zone.from_local_datetime(&naive).earliest())
}

fn previous_day(day: NaiveDate) -> NaiveDate {
    day.checked_sub_days(Days::new(1)).unwrap_or(day)
}

fn day_heading(day: NaiveDate, today: NaiveDate) -> String {
    if day == today {
        "Today".into()
    } else if day == previous_day(today) {
        "Yesterday".into()
    } else if day.year() == today.year() {
        day.format("%-d %B").to_string()
    } else {
        short_date(day)
    }
}

/// Chat-list stamp for an epoch-milliseconds instant: today → short time,
/// yesterday → "Yesterday", within a week → weekday, this year → "12 Jun",
/// older → short date. Empty for a missing or zero instant.
pub fn chat_stamp<Tz: TimeZone>(epoch_millis: i64, now: &DateTime<Tz>) -> String
where
    Tz::Offset: std::fmt::Display,
{
    if epoch_millis <= 0 {
        return String::new();
    }
    let Some(moment) = now.timezone().timestamp_millis_opt(epoch_millis).single() else {
        return String::new();
    };
    let (day, today) = (moment.date_naive(), now.date_naive());
    if day == today {
        short_time(&moment)
    } else if day == previous_day(today) {
        "Yesterday".into()
    } else if day < today && (today - day).num_days() < 7 {
        day.format("%A").to_string()
    } else if day.year() == today.year() {
        day.format("%-d %b").to_string()
    } else {
        short_date(day)
    }
}

/// Short clock time for an ISO-8601 message timestamp, or empty.
pub fn clock_time<Tz: TimeZone>(timestamp: &str, zone: &Tz) -> String
where
    Tz::Offset: std::fmt::Display,
{
    parse_iso(timestamp, zone).map(|m| short_time(&m)).unwrap_or_default()
}

/// Heading for the day a message belongs to, or empty when it falls on the
/// same day as `previous`. An unparseable timestamp never opens a day.
pub fn day_separator<Tz: TimeZone>(timestamp: &str, previous: &str, now: &DateTime<Tz>) -> String {
    let zone = now.timezone();
    let Some(moment) = parse_iso(timestamp, &zone) else {
        return String::new();
    };
    if parse_iso(previous, &zone).is_some_and(|p| p.date_naive() == moment.date_naive()) {
        return String::new();
    }
    day_heading(moment.date_naive(), now.date_naive())
}

/// Compact running time: "12s", "4m", "1h 05m", "3d". Negative reads "0s".
pub fn compact_duration(milliseconds: i64) -> String {
    let seconds = (milliseconds / 1000).max(0);
    if seconds < 60 {
        return format!("{seconds}s");
    }
    let minutes = seconds / 60;
    if minutes < 60 {
        return format!("{minutes}m");
    }
    let hours = minutes / 60;
    if hours < 24 {
        return format!("{hours}h {:02}m", minutes % 60);
    }
    format!("{}d", hours / 24)
}
