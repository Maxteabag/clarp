//! KEPT PROCESSES: the connected Host's settings of lanes (feature
//! `lane_settings`, Rust Host contract 178), listed only where the Host
//! offers them. One switch per tool the Host names (every `*_chat_lane`
//! key, so a tool the Host adds shows without a change here), the three
//! numbers with the ranges the Host gives, and what the lanes hold now as
//! information below them (`settings_view`). Peer Hosts are not shown.

use clarp_engine::lanes;
use serde_json::Value;

use super::{Setting, e};
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

/// "codex" → "Codex".
fn tool_name(tool: &str) -> String {
    let mut chars = tool.chars();
    chars.next().map(|first| first.to_uppercase().chain(chars).collect()).unwrap_or_default()
}

fn number(app: &App, key: &str) -> f64 {
    app.engine.borrow().lane_settings().and_then(|answer| lanes::number(answer, key)).unwrap_or(0.0)
}

fn range(app: &App, key: &str) -> Option<(f64, f64)> {
    app.engine.borrow().lane_settings().and_then(|answer| lanes::range(answer, key))
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
    .label_from(move |app| switch(app, slot).map_or_else(String::new, |s| format!("{} kept between messages", tool_name(&s.tool))))
    .when(move |app| offered(app) && switch(app, slot).is_some())
}

#[allow(clippy::too_many_arguments)]
fn limit(id: &'static str, label: &'static str, description: &'static str, aliases: &'static [&'static str], key: &'static str, fallback: (f64, f64), step: f64, unit: &'static str) -> Setting {
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
    .when(offered)
}

pub(super) fn register() -> Vec<Setting> {
    let mut all: Vec<Setting> = (0..SLOTS.len()).map(tool_switch).collect();
    all.push(limit(
        "kept-most",
        "Most kept processes",
        "How many kept processes the connected Host holds, of every tool together.",
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
    let mut out = vec![("Now".to_owned(), now)];
    if !engine.lane_settings_error().is_empty() {
        out.push(("Not changed".to_owned(), engine.lane_settings_error().to_owned()));
    }
    out.push(("When".to_owned(), "Changes take effect at each agent's next message.".to_owned()));
    out
}

#[cfg(test)]
mod tests {
    #[test]
    fn a_tools_name_starts_with_a_capital() {
        assert_eq!(super::tool_name("codex"), "Codex");
        assert_eq!(super::tool_name(""), "");
    }
}
