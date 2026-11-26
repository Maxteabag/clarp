//! KEPT PROCESSES: the connected Host's settings of lanes (feature
//! `lane_settings`, Rust Host contract 178), listed only where the Host
//! offers them. One switch per tool the Host names (every `*_chat_lane`
//! key, so a tool the Host adds shows without a change here), the three
//! numbers with the ranges the Host gives, and what the lanes hold now as
//! information below them (`settings_view`). Peer Hosts are not shown.
//!
//! Where the Host also offers `lane_auto_limits` (contract 180): how the
//! limits are set (auto or fixed), in auto the memory kept free and the
//! most kept processes, and what the machine has free. In auto the two
//! fixed limits stay listed, marked as the fixed limits: setting either
//! switches the Host to fixed.
//!
//! Where the Host also offers `turn_pressure_admission` (contract 186): the
//! three pressure thresholds as settings, and below them what loads the
//! machine in three groups (live processes, cold starts in the last hour,
//! pressure with the turns held), each with a line per tool or reading. The
//! most kept in auto bounds nothing there and is not listed.

use clarp_engine::lanes;
use serde_json::Value;

use super::{Choice, Setting, e};
use crate::App;

pub const SECTION: &str = "KEPT PROCESSES";

/// Tools a Host may name; one more than any Host has today leaves room.
const SLOTS: [&str; 6] = ["kept-tool-1", "kept-tool-2", "kept-tool-3", "kept-tool-4", "kept-tool-5", "kept-tool-6"];

fn offered(app: &App) -> bool {
    let engine = app.engine.borrow();
    engine.lane_settings_offered() && engine.lane_settings().is_some()
}

/// The Host's switch in this slot, by key order.
fn switch(app: &App, slot: usize) -> Option<lanes::LaneSwitch> {
    app.engine.borrow().lane_settings().and_then(|answer| lanes::switches(answer).into_iter().nth(slot))
}

fn number(app: &App, key: &str) -> f64 {
    app.engine.borrow().lane_settings().and_then(|answer| lanes::number(answer, key)).unwrap_or(0.0)
}

fn range(app: &App, key: &str) -> Option<(f64, f64)> {
    app.engine.borrow().lane_settings().and_then(|answer| lanes::range(answer, key))
}

/// The Host offers auto limits and has said how they are set.
fn auto_offered(app: &App) -> bool {
    offered(app) && app.engine.borrow().lane_auto_limits_offered()
}

/// The Host says what loads its machine and holds turns while it is under pressure.
/// The Host's limits follow its free memory now.
fn in_auto(app: &App) -> bool {
    auto_offered(app) && app.engine.borrow().lane_settings().and_then(lanes::limits) == Some(lanes::AUTO)
}

fn set_number(app: &App, key: &str, value: f64) {
    app.engine.borrow_mut().set_lane_setting(key, Value::from(value.round() as i64));
}

fn tool_switch(slot: usize) -> Setting {
    Setting::toggle(
        e(
            SLOTS[slot],
            "Kept between messages",
            "host",
            "Keeps this tool running between an agent's messages on the connected Host, so the next message starts sooner.",
            &["kept processes", "lanes", "keep running", "warm", "background process", "tool process", "toggle"],
        ),
        SECTION,
        move |app| switch(app, slot).is_some_and(|s| s.on),
        move |app, _, on| {
            if let Some(found) = switch(app, slot) {
                app.engine.borrow_mut().set_lane_setting(&found.key, Value::Bool(on));
            }
        },
    )
    .label_from(move |app| switch(app, slot).map_or_else(String::new, |s| format!("{} kept between messages", lanes::tool_name(&s.tool))))
    .when(move |app| offered(app) && switch(app, slot).is_some())
}

#[allow(clippy::too_many_arguments)]
fn limit(id: &'static str, label: &'static str, description: &'static str, aliases: &'static [&'static str], key: &'static str, fallback: (f64, f64), step: f64, unit: &'static str) -> Setting {
    let shown = move |app: &App| {
        let value = super::with_unit(&super::number_text(number(app, key), step), unit);
        // In auto a fixed limit is not used: said beside it.
        if lanes::FIXED_NUMBERS.contains(&key) && in_auto(app) { format!("{value} · fixed limit, setting it switches to fixed") } else { value }
    };
    Setting::number(
        e(id, label, "host", description, aliases),
        SECTION,
        move |app| number(app, key),
        move |app, _, value| set_number(app, key, value),
        fallback.0,
        fallback.1,
        step,
        unit,
    )
    .range_from(move |app| range(app, key))
    .detail(shown)
    .when(offered)
}

fn limits_choice() -> Setting {
    Setting::choice(
        e(
            "kept-limits",
            "Limits",
            "host",
            "Auto keeps processes while the connected Host's machine keeps enough memory free; fixed holds to the most and the memory set below.",
            &["auto", "fixed", "limits", "free memory", "dynamic", "kept processes", "lanes", "mode"],
        ),
        SECTION,
        |_| {
            vec![
                Choice::new(lanes::AUTO, "Auto").about("Follow the machine's free memory"),
                Choice::new(lanes::FIXED, "Fixed").about("Most kept processes and the memory they may hold"),
            ]
        },
        |app| app.engine.borrow().lane_settings().and_then(lanes::limits).unwrap_or_default().to_owned(),
        |app, _, value| app.engine.borrow_mut().set_lane_setting(lanes::LIMITS, Value::from(value)),
    )
    .when(auto_offered)
}

#[allow(clippy::too_many_arguments)]
/// A whole number the Host keeps, stepped by one within its range.
fn host_number(id: &'static str, label: &'static str, description: &'static str, aliases: &'static [&'static str], key: &'static str, fallback: (f64, f64), unit: &'static str) -> Setting {
    Setting::number(e(id, label, "host", description, aliases), SECTION, move |app| number(app, key), move |app, _, value| set_number(app, key, value), fallback.0, fallback.1, 1.0, unit)
        .range_from(move |app| range(app, key))
}

pub(super) fn register() -> Vec<Setting> {
    let mut all: Vec<Setting> = (0..SLOTS.len()).map(tool_switch).collect();
    all.push(limits_choice());
    all.push(host_number(
        "kept-reserve",
        "Memory kept free",
        "In auto, how much of the machine's memory the connected Host keeps free: a process is kept only while this much stays free.",
        &["reserve", "reserve percent", "free memory", "headroom", "auto", "kept processes"],
        "reserve_percent",
        (5.0, 50.0),
        "%",
    )
    .when(in_auto));
    all.push(host_number(
        "kept-ceiling",
        "Most kept in auto",
        "In auto, how many kept processes the connected Host holds at most, however much memory is free.",
        // ui-text: allow (search aliases, never drawn: older words people may still type)
        &["ceiling", "cap", "most lanes", "auto", "how many", "kept processes", "maximum"],
        "ceiling",
        (1.0, 64.0),
        "",
    )
    // Where the Host says which processes live, the ceiling bounds nothing.
    .when(|app| in_auto(app) && !app.engine.borrow().lane_live_offered()));
    all.push(limit(
        "kept-most",
        "Most kept processes",
        "How many kept processes the connected Host holds, of every tool together.",
        // ui-text: allow (search aliases, never drawn: older words people may still type)
        &["lanes", "most lanes", "limit", "how many", "kept processes", "maximum"],
        "most",
        (1.0, 32.0),
        1.0,
        "",
    ));
    all.push(limit(
        "kept-memory",
        "Memory they may hold",
        "How much memory the kept processes may hold together on the connected Host.",
        // ui-text: allow (search aliases, never drawn: older words people may still type)
        &["memory", "ram", "lane memory", "budget", "kept processes", "megabytes"],
        "memory_mb",
        (256.0, 16384.0),
        128.0,
        "MB",
    ));
    all.push(limit(
        "kept-idle",
        "Unused one kept for",
        "How long a kept process with no message is kept before it leaves.",
        &["idle", "timeout", "idle minutes", "how long", "kept processes", "unused"],
        "idle_minutes",
        (1.0, 240.0),
        1.0,
        "min",
    ));
    all
}

/// What the section shows below its settings: what the lanes hold now, a
/// refusal's sentence as the Host said it, and when a change applies.
pub fn information(app: &App) -> Vec<(String, String)> {
    let engine = app.engine.borrow();
    if !engine.lane_settings_offered() {
        return Vec::new();
    }
    let now = engine.lane_settings().map(lanes::held_line).filter(|line| !line.is_empty()).unwrap_or_else(|| "Loading…".to_owned());
    let mut out = Vec::new();
    let auto = engine.lane_auto_limits_offered() && engine.lane_settings().and_then(lanes::limits) == Some(lanes::AUTO);
    if auto {
        let free = engine.lane_settings().map(lanes::free_line).filter(|line| !line.is_empty());
        out.push(("Free memory".to_owned(), free.unwrap_or_else(|| "The Host has not said what its machine has free".to_owned())));
    }
    let groups = if engine.lane_live_offered() { engine.lane_settings().map(groups).unwrap_or_default() } else { Vec::new() };
    if groups.is_empty() {
        out.push(("Now".to_owned(), now));
    }
    for group in groups {
        out.push((group.title, group.summary));
        out.extend(group.lines.into_iter().map(|(label, detail)| (format!("{TOOL_INDENT}{label}"), detail)));
    }
    if auto {
        out.push(("Fixed limits".to_owned(), "Most kept processes and Memory they may hold apply when the limits are fixed; setting either switches the Host to fixed.".to_owned()));
    }
    if !engine.lane_settings_error().is_empty() {
        out.push(("Not changed".to_owned(), engine.lane_settings_error().to_owned()));
    }
    out.push(("When".to_owned(), "Changes take effect at each agent's next message.".to_owned()));
    out
}

/// A line under a group starts so, to read as part of it.
pub const TOOL_INDENT: &str = "    ";

/// What loads the connected Host's machine, the groups it says: live
/// processes, cold starts in the last hour, and pressure.
fn groups(answer: &serde_json::Map<String, Value>) -> Vec<lanes::Group> {
    [lanes::live_group(answer), lanes::cold_starts_group(answer)].into_iter().flatten().collect()
}
