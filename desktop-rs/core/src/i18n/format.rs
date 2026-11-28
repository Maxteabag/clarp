//! Dates, times, numbers and relative times in a language, for every text
//! that shows one. Each takes the language's tag (`i18n::current()` for the
//! app's own); never build weekday or month names, "ago" phrases or number
//! separators by hand.
//!
//! Dates, times and numbers come from ICU4X with its compiled CLDR data:
//! Slint already carries ICU's number and plural data, and the date data adds
//! about 1.3 MB. Relative times ("5 minutes ago", "Yesterday") are catalog
//! strings with plurals, because ICU's relative-time formatter is still
//! experimental and pins an older ICU that would ship twice.

use std::cell::RefCell;
use std::collections::HashMap;

use chrono::{Datelike, NaiveDate, NaiveDateTime, NaiveTime, Timelike};
use icu_datetime::fieldsets;
use icu_datetime::{DateTimeFormatter, DateTimeFormatterPreferences, NoCalendarFormatter};
use icu_decimal::DecimalFormatter;
use icu_decimal::input::Decimal;
use icu_locale_core::Locale;
use icu_locale_core::preferences::extensions::unicode::keywords::HourCycle;

use super::translate_in;

/// The ICU locale of a shipped language's tag (English for anything else).
pub fn locale(tag: &str) -> Locale {
    tag.parse().unwrap_or_else(|_| "en".parse().expect("en is a locale"))
}

/// Which clock times use: the language's own, or the user's 12- or 24-hour choice.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Clock {
    Locale,
    H12,
    H24,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum Pattern {
    /// "Thursday", "torsdag", "星期四", "गुरुवार".
    Weekday,
    /// "Nov 26", "26. nov.", "11月26日".
    DayMonth,
    /// "November 26", "26. november".
    DayMonthLong,
    /// "Nov 26, 2026", "26. nov. 2026".
    Date,
    /// "11/26/26", "26.11.2026".
    DateShort,
}

thread_local! {
    static DATES: RefCell<HashMap<(String, Pattern), DateTimeFormatter<fieldsets::enums::DateFieldSet>>> = RefCell::new(HashMap::new());
    static TIMES: RefCell<HashMap<(String, Clock), NoCalendarFormatter<fieldsets::T>>> = RefCell::new(HashMap::new());
    static NUMBERS: RefCell<HashMap<String, DecimalFormatter>> = RefCell::new(HashMap::new());
    static MOMENTS: RefCell<HashMap<(String, Moment, Clock), DateTimeFormatter<fieldsets::enums::DateAndTimeFieldSet>>> = RefCell::new(HashMap::new());
}

fn iso(day: NaiveDate) -> Option<icu_calendar::Date<icu_calendar::Iso>> {
    icu_calendar::Date::try_new_iso(day.year(), day.month() as u8, day.day() as u8).ok()
}

fn date_with(tag: &str, day: NaiveDate, pattern: Pattern) -> String {
    let Some(date) = iso(day) else { return String::new() };
    DATES.with(|cache| {
        let mut cache = cache.borrow_mut();
        let key = (tag.to_owned(), pattern);
        if !cache.contains_key(&key) {
            let prefs = DateTimeFormatterPreferences::from(&locale(tag));
            let made = match pattern {
                Pattern::Weekday => DateTimeFormatter::try_new(prefs, fieldsets::E::long()).map(|f| f.cast_into_fset()),
                Pattern::DayMonth => DateTimeFormatter::try_new(prefs, fieldsets::MD::medium()).map(|f| f.cast_into_fset()),
                Pattern::DayMonthLong => DateTimeFormatter::try_new(prefs, fieldsets::MD::long()).map(|f| f.cast_into_fset()),
                Pattern::Date => DateTimeFormatter::try_new(prefs, fieldsets::YMD::medium()).map(|f| f.cast_into_fset()),
                Pattern::DateShort => DateTimeFormatter::try_new(prefs, fieldsets::YMD::short()).map(|f| f.cast_into_fset()),
            };
            match made {
                Ok(formatter) => {
                    cache.insert(key.clone(), formatter);
                }
                Err(_) => return day.format("%Y-%m-%d").to_string(),
            }
        }
        cache[&key].format(&date).to_string()
    })
}

/// The day of the week: "Thursday", "torsdag", "星期四".
pub fn weekday(tag: &str, day: NaiveDate) -> String {
    date_with(tag, day, Pattern::Weekday)
}

/// Day and short month, no year: "Nov 26", "26. nov.", "11月26日".
pub fn day_month(tag: &str, day: NaiveDate) -> String {
    date_with(tag, day, Pattern::DayMonth)
}

/// Day and full month, no year: "November 26", "26. november".
pub fn day_month_long(tag: &str, day: NaiveDate) -> String {
    date_with(tag, day, Pattern::DayMonthLong)
}

/// A date with its year: "Nov 26, 2026", "26. nov. 2026", "2026年11月26日".
pub fn date(tag: &str, day: NaiveDate) -> String {
    date_with(tag, day, Pattern::Date)
}

/// A date in digits: "11/26/26", "26.11.2026", "2026/11/26".
pub fn date_short(tag: &str, day: NaiveDate) -> String {
    date_with(tag, day, Pattern::DateShort)
}

/// A clock time in hours and minutes: "2:05 PM", "14:05".
pub fn time(tag: &str, at: NaiveTime, clock: Clock) -> String {
    let Ok(value) = icu_time::Time::try_new(at.hour() as u8, at.minute() as u8, 0, 0) else { return String::new() };
    TIMES.with(|cache| {
        let mut cache = cache.borrow_mut();
        let key = (tag.to_owned(), clock);
        if !cache.contains_key(&key) {
            let mut prefs = DateTimeFormatterPreferences::from(&locale(tag));
            prefs.hour_cycle = match clock {
                Clock::Locale => None,
                Clock::H12 => Some(HourCycle::H12),
                Clock::H24 => Some(HourCycle::H23),
            };
            match NoCalendarFormatter::try_new(prefs, fieldsets::T::hm()) {
                Ok(formatter) => {
                    cache.insert(key.clone(), formatter);
                }
                Err(_) => return at.format("%H:%M").to_string(),
            }
        }
        cache[&key].format(&value).to_string()
    })
}

/// How far a moment is from today, for `moment`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum Moment {
    /// "Sat 6:27 PM", "lør. 18:27".
    WeekdayTime,
    /// "Dec 5, 6:27 PM", "5. des., 18:27".
    DayMonthTime,
    /// "Dec 5, 2027, 6:27 PM".
    DateTime,
}

/// A moment told against today, as a reset or a scheduled time reads: the
/// time alone when it is today ("6:27 PM", "18:27"), with the weekday within
/// a week ("Sat 6:27 PM"), else with the date ("Dec 5, 6:27 PM"; the year
/// when it is another one).
pub fn moment(tag: &str, at: NaiveDateTime, today: NaiveDate, clock: Clock) -> String {
    let days = (at.date() - today).num_days();
    if days == 0 {
        return time(tag, at.time(), clock);
    }
    let kind = if days.abs() < 7 {
        Moment::WeekdayTime
    } else if at.year() == today.year() {
        Moment::DayMonthTime
    } else {
        Moment::DateTime
    };
    let (Some(date), Ok(clock_time)) = (iso(at.date()), icu_time::Time::try_new(at.hour() as u8, at.minute() as u8, 0, 0)) else { return String::new() };
    let value = icu_time::DateTime { date, time: clock_time };
    MOMENTS.with(|cache| {
        let mut cache = cache.borrow_mut();
        let key = (tag.to_owned(), kind, clock);
        if !cache.contains_key(&key) {
            let mut prefs = DateTimeFormatterPreferences::from(&locale(tag));
            prefs.hour_cycle = match clock {
                Clock::Locale => None,
                Clock::H12 => Some(HourCycle::H12),
                Clock::H24 => Some(HourCycle::H23),
            };
            let made = match kind {
                Moment::WeekdayTime => DateTimeFormatter::try_new(prefs, fieldsets::E::short().with_time_hm()).map(|f| f.cast_into_fset()),
                Moment::DayMonthTime => DateTimeFormatter::try_new(prefs, fieldsets::MD::medium().with_time_hm()).map(|f| f.cast_into_fset()),
                Moment::DateTime => DateTimeFormatter::try_new(prefs, fieldsets::YMD::medium().with_time_hm()).map(|f| f.cast_into_fset()),
            };
            match made {
                Ok(formatter) => {
                    cache.insert(key.clone(), formatter);
                }
                Err(_) => return at.format("%Y-%m-%d %H:%M").to_string(),
            }
        }
        cache[&key].format(&value).to_string()
    })
}

fn with_numbers<T>(tag: &str, act: impl FnOnce(&DecimalFormatter) -> T, fallback: impl FnOnce() -> T) -> T {
    NUMBERS.with(|cache| {
        let mut cache = cache.borrow_mut();
        if !cache.contains_key(tag) {
            match DecimalFormatter::try_new((&locale(tag)).into(), Default::default()) {
                Ok(formatter) => {
                    cache.insert(tag.to_owned(), formatter);
                }
                Err(_) => return fallback(),
            }
        }
        act(&cache[tag])
    })
}

/// A whole number with the language's grouping: "1,234,567", "1 234 567",
/// "12,34,567" (Hindi), "1234" (Spanish groups from five digits).
pub fn number(tag: &str, value: i64) -> String {
    with_numbers(tag, |f| f.format(&Decimal::from(value)).to_string(), || value.to_string())
}

/// A number with `places` decimals: "1,234.5", "1 234,5".
pub fn decimal(tag: &str, value: f64, places: u8) -> String {
    let text = format!("{value:.*}", usize::from(places));
    match text.parse::<Decimal>() {
        Ok(parsed) => with_numbers(tag, |f| f.format(&parsed).to_string(), || text.clone()),
        Err(_) => text,
    }
}

/// "Today", "Yesterday" or "Tomorrow" for a day near `today`, else None.
pub fn relative_day(tag: &str, day: NaiveDate, today: NaiveDate) -> Option<String> {
    match (day - today).num_days() {
        0 => Some(translate_in(tag, "", "Today", "", None, &[])),
        -1 => Some(translate_in(tag, "", "Yesterday", "", None, &[])),
        1 => Some(translate_in(tag, "", "Tomorrow", "", None, &[])),
        _ => None,
    }
}

/// How long ago (`seconds` negative) or how far ahead an instant is, in the
/// largest whole unit: "Just now", "5 minutes ago", "in 2 hours", "3 days
/// ago", "2 weeks ago", "4 months ago", "1 year ago". A month is 30 days.
pub fn relative(tag: &str, seconds: i64) -> String {
    let past = seconds < 0;
    let span = seconds.unsigned_abs() as i64;
    if span < 45 {
        return translate_in(tag, "", "Just now", "", None, &[]);
    }
    let (n, unit) = match span {
        s if s < 3600 => ((s + 30) / 60, 0),
        s if s < 86_400 => (s / 3600, 1),
        s if s < 7 * 86_400 => (s / 86_400, 2),
        s if s < 30 * 86_400 => (s / (7 * 86_400), 3),
        s if s < 365 * 86_400 => (s / (30 * 86_400), 4),
        s => (s / (365 * 86_400), 5),
    };
    let n = n.max(1);
    // Each phrase is a whole catalog string so every language orders its own words.
    let (one, other) = match (past, unit) {
        (true, 0) => ("{n} minute ago", "{n} minutes ago"),
        (true, 1) => ("{n} hour ago", "{n} hours ago"),
        (true, 2) => ("{n} day ago", "{n} days ago"),
        (true, 3) => ("{n} week ago", "{n} weeks ago"),
        (true, 4) => ("{n} month ago", "{n} months ago"),
        (true, _) => ("{n} year ago", "{n} years ago"),
        (false, 0) => ("in {n} minute", "in {n} minutes"),
        (false, 1) => ("in {n} hour", "in {n} hours"),
        (false, 2) => ("in {n} day", "in {n} days"),
        (false, 3) => ("in {n} week", "in {n} weeks"),
        (false, 4) => ("in {n} month", "in {n} months"),
        (false, _) => ("in {n} year", "in {n} years"),
    };
    translate_in(tag, "", one, other, Some(n), &[])
}

/// A unit pattern for a short duration; each language abbreviates its own units.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Span {
    /// "12s"
    Seconds,
    /// "4m"
    Minutes,
    /// "6m 39s"
    MinutesSeconds,
    /// "1h 05m"
    HoursMinutes,
    /// "3d"
    Days,
    /// "5d 5h"
    DaysHours,
}

/// A short duration from already-formatted parts (`b` only for the two-part
/// spans): "12s", "6m 39s", "1h 05m", "3d". The caller picks the unit and any
/// zero padding; the language writes the unit letters and their order.
pub fn span(tag: &str, span: Span, a: &str, b: &str) -> String {
    let template = match span {
        Span::Seconds => "{0}s",
        Span::Minutes => "{0}m",
        Span::MinutesSeconds => "{0}m {1}s",
        Span::HoursMinutes => "{0}h {1}m",
        Span::Days => "{0}d",
        Span::DaysHours => "{0}d {1}h",
    };
    translate_in(tag, "duration", template, "", None, &[&a as &dyn std::fmt::Display, &b])
}

/// The catalog strings this module uses: `relative` picks them at run time,
/// so tools/i18n-export finds them here.
#[allow(dead_code)]
// i18n-screen: dates
fn catalog_strings() -> Vec<String> {
    use crate::tr;
    vec![
        // i18n: day heading in the transcript and chat-list stamp for a message sent today (adverb, capitalised as a heading)
        tr!("Today"),
        // i18n: day heading in the transcript and chat-list stamp for a message sent yesterday
        tr!("Yesterday"),
        // i18n: day heading for something due tomorrow
        tr!("Tomorrow"),
        // i18n: relative time for something under a minute ago, shown alone as a timestamp
        tr!("Just now"),
        // i18n: relative timestamp in lists and tooltips, how long ago something happened; {n} is the number
        tr!("{n} minute ago" | "{n} minutes ago" % 1),
        // i18n: relative timestamp in lists and tooltips, how long ago something happened; {n} is the number
        tr!("{n} hour ago" | "{n} hours ago" % 1),
        // i18n: relative timestamp in lists and tooltips, how long ago something happened; {n} is the number
        tr!("{n} day ago" | "{n} days ago" % 1),
        // i18n: relative timestamp in lists and tooltips, how long ago something happened; {n} is the number
        tr!("{n} week ago" | "{n} weeks ago" % 1),
        // i18n: relative timestamp in lists and tooltips, how long ago something happened; {n} is the number
        tr!("{n} month ago" | "{n} months ago" % 1),
        // i18n: relative timestamp in lists and tooltips, how long ago something happened; {n} is the number
        tr!("{n} year ago" | "{n} years ago" % 1),
        // i18n: relative timestamp for something due in the future; {n} is the number
        tr!("in {n} minute" | "in {n} minutes" % 1),
        // i18n: relative timestamp for something due in the future; {n} is the number
        tr!("in {n} hour" | "in {n} hours" % 1),
        // i18n: relative timestamp for something due in the future; {n} is the number
        tr!("in {n} day" | "in {n} days" % 1),
        // i18n: relative timestamp for something due in the future; {n} is the number
        tr!("in {n} week" | "in {n} weeks" % 1),
        // i18n: relative timestamp for something due in the future; {n} is the number
        tr!("in {n} month" | "in {n} months" % 1),
        // i18n: relative timestamp for something due in the future; {n} is the number
        tr!("in {n} year" | "in {n} years" % 1),
        // i18n: short duration in seconds, e.g. "12s" (how long a turn or a tool has run, chat list and transcript); {0} is the number; use the language's usual one-letter or short unit
        tr!("duration" => "{0}s", 1),
        // i18n: short duration in minutes, e.g. "4m" (how long something has run); {0} is the number; use the language's usual short unit
        tr!("duration" => "{0}m", 1),
        // i18n: short duration in minutes and seconds, e.g. "6m 39s" (how long a turn has run); {0} minutes, {1} seconds
        tr!("duration" => "{0}m {1}s", 1, 2),
        // i18n: short duration in hours and minutes, e.g. "1h 05m" (how long a turn has run); {0} hours, {1} minutes
        tr!("duration" => "{0}h {1}m", 1, 2),
        // i18n: short duration in days, e.g. "3d" (how long a process has run); {0} is the number; use the language's usual short unit
        tr!("duration" => "{0}d", 1),
        // i18n: short duration in days and hours, e.g. "5d 5h" (time until a usage limit resets); {0} days, {1} hours
        tr!("duration" => "{0}d {1}h", 1, 2),
    ]
}
