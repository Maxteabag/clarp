//! Chat-list stamps, transcript day headings and running durations, in the
//! app's language (`i18n::format`). `now` is injected so tests are
//! deterministic; instants are shown in `now`'s time zone.

use chrono::{DateTime, Datelike, Days, FixedOffset, NaiveDate, NaiveDateTime, TimeZone};

use crate::i18n::{self, format};

static CLOCK_24H: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Settings → Time format: times as "18:10" instead of "6:10 PM".
pub fn set_clock_24h(on: bool) {
    CLOCK_24H.store(on, std::sync::atomic::Ordering::Relaxed);
}

fn short_time<Tz: TimeZone>(moment: &DateTime<Tz>) -> String
where
    Tz::Offset: std::fmt::Display,
{
    let clock = if CLOCK_24H.load(std::sync::atomic::Ordering::Relaxed) { format::Clock::H24 } else { format::Clock::H12 };
    format::time(i18n::current(), moment.time(), clock)
}

fn short_date(day: NaiveDate) -> String {
    format::date_short(i18n::current(), day)
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

/// An ISO-8601 instant as milliseconds since the epoch (UTC without an
/// offset, as the Host writes them).
pub fn epoch_ms(timestamp: &str) -> Option<i64> {
    parse_iso(timestamp, &chrono::Utc).map(|t| t.timestamp_millis())
}

fn previous_day(day: NaiveDate) -> NaiveDate {
    day.checked_sub_days(Days::new(1)).unwrap_or(day)
}

fn day_heading(day: NaiveDate, today: NaiveDate) -> String {
    if day == today || day == previous_day(today) {
        format::relative_day(i18n::current(), day, today).unwrap_or_default()
    } else if day.year() == today.year() {
        format::day_month_long(i18n::current(), day)
    } else {
        short_date(day)
    }
}

/// Chat-list stamp for an epoch-milliseconds instant: today → short time,
/// yesterday → "Yesterday", within a week → weekday, this year → "Jun 12",
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
        format::relative_day(i18n::current(), day, today).unwrap_or_default()
    } else if day < today && (today - day).num_days() < 7 {
        format::weekday(i18n::current(), day)
    } else if day.year() == today.year() {
        format::day_month(i18n::current(), day)
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

fn number(n: i64) -> String {
    format::number(i18n::current(), n)
}

/// Compact running time: "12s", "4m", "1h 05m", "3d". Negative reads "0s".
pub fn compact_duration(milliseconds: i64) -> String {
    let tag = i18n::current();
    let seconds = (milliseconds / 1000).max(0);
    if seconds < 60 {
        return format::span(tag, format::Span::Seconds, &number(seconds), "");
    }
    let minutes = seconds / 60;
    if minutes < 60 {
        return format::span(tag, format::Span::Minutes, &number(minutes), "");
    }
    let hours = minutes / 60;
    if hours < 24 {
        return format::span(tag, format::Span::HoursMinutes, &number(hours), &format!("{:02}", minutes % 60));
    }
    format::span(tag, format::Span::Days, &number(hours / 24), "")
}

/// A running turn's time for the chat list, ticking each second: "12s",
/// "6m 39s", "1h 05m". A start ahead of the local clock reads "0s".
pub fn turn_elapsed(milliseconds: i64) -> String {
    let tag = i18n::current();
    let seconds = (milliseconds / 1000).max(0);
    if seconds < 60 {
        return format::span(tag, format::Span::Seconds, &number(seconds), "");
    }
    if seconds < 3600 {
        return format::span(tag, format::Span::MinutesSeconds, &number(seconds / 60), &format!("{:02}", seconds % 60));
    }
    format::span(tag, format::Span::HoursMinutes, &number(seconds / 3600), &format!("{:02}", seconds / 60 % 60))
}
