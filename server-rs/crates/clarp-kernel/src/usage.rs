//! What a Host's own turns used.
//!
//! Every tool ends a turn by saying what the turn took in and gave out, in tokens, and
//! some say what it cost. The Host keeps that for each turn, and an app's usage screen is
//! told the sums over the last five hours and the last seven days: how many turns, what
//! they took in and gave out, what they cost where a tool said so. It is what was spent
//! through this Host, and not how much of an account is left: no tool says that at the end
//! of a turn.
//!
//! The rules are the Python Host's (`server/lib/turn_usage.py`, and the row an app is
//! given, `backend_usage_claude.py`, `_claude_response_row`), held by
//! `tests/fixtures/usage-vectors.json`.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

/// The windows the sums are told for: their names, as an app knows them, and how long
/// each is, in milliseconds. They are named after the windows providers count in and are
/// plain spans of time back from now.
pub const WINDOWS: [(&str, i64); 2] = [
    ("five_hour", 5 * 60 * 60 * 1_000),
    ("seven_day", 7 * 24 * 60 * 60 * 1_000),
];
/// How long what a turn used is kept: as long as the longest window.
pub const KEPT_FOR_MS: i64 = WINDOWS[1].1;
/// How old the newest turn may be for the sums to be told as fresh.
pub const FRESH_FOR_MS: i64 = 60 * 60 * 1_000;
/// Where the sums come from, as an app is told.
pub const SOURCE: &str = "clarp-turn-accounting";
/// What is said where no turn of a tool was counted yet.
pub const NONE_YET: &str = "no turns recorded yet";

/// What one turn used, as its tool said when the turn ended.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Used {
    /// The tool the turn ran on.
    pub tool: String,
    /// When the turn ended.
    pub at_ms: i64,
    pub tokens_in: u64,
    pub tokens_out: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cost_usd: Option<f64>,
}

impl Used {
    /// What a turn used, from what its tool said of it. `None` where the tool said
    /// nothing of the three: a turn that failed early says none, and is not counted.
    pub fn said(
        tool: &str,
        at_ms: i64,
        tokens_in: Option<u64>,
        tokens_out: Option<u64>,
        cost_usd: Option<f64>,
    ) -> Option<Self> {
        if tokens_in.is_none() && tokens_out.is_none() && cost_usd.is_none() {
            return None;
        }
        Some(Self {
            tool: tool.to_string(),
            at_ms,
            tokens_in: tokens_in.unwrap_or(0),
            tokens_out: tokens_out.unwrap_or(0),
            cost_usd,
        })
    }
}

/// A tool's sums over each window, and when its newest turn ended (`last_turn_at`; `null`
/// where none is kept). A window holds the turns that ended less than its length ago. The
/// cost of a window is told only where a turn in it said one.
pub fn totals<'a>(turns: impl IntoIterator<Item = &'a Used>, tool: &str, now_ms: i64) -> Value {
    let of_the_tool: Vec<&Used> = turns.into_iter().filter(|used| used.tool == tool).collect();
    let mut windows = Map::new();
    for (name, span_ms) in WINDOWS {
        let within = || {
            of_the_tool
                .iter()
                .filter(|used| used.at_ms > now_ms - span_ms)
        };
        let costs: Vec<f64> = within().filter_map(|used| used.cost_usd).collect();
        let cost = (!costs.is_empty()).then(|| costs.iter().sum::<f64>());
        let window = json!({
            "turns": within().count(),
            "tokens_in": within().map(|used| used.tokens_in).sum::<u64>(),
            "tokens_out": within().map(|used| used.tokens_out).sum::<u64>(),
            "cost_usd": cost,
            "window_minutes": span_ms / 60_000,
        });
        windows.insert(name.to_string(), window);
    }
    let newest = of_the_tool.iter().map(|used| used.at_ms).max();
    json!({ "windows": windows, "last_turn_at": newest.filter(|at| *at != 0) })
}

/// The row an app's usage screen is given for a tool, from its sums: no share of an
/// account (`used_percentage` and `resets_at` are `null`, `windows` empty, so that an app
/// written for a quota shows that it is not known), the sums as `totals`, and how fresh
/// they are by the age of the newest turn. `ended_at`: the moment the newest turn ended as
/// the Host writes a moment, where there is one.
pub fn row(tool: &str, totals: &Value, now_ms: i64, ended_at: Option<String>) -> Value {
    let newest = totals["last_turn_at"].as_i64();
    let freshness = match newest {
        None => "unknown",
        Some(at) if now_ms - at <= FRESH_FOR_MS => "fresh",
        Some(_) => "stale",
    };
    json!({
        "backend": tool,
        "used_percentage": null,
        "resets_at": null,
        "source": SOURCE,
        "fetched_at": newest.and(ended_at),
        "freshness": freshness,
        "error": if newest.is_some() { "" } else { NONE_YET },
        "windows": {},
        "totals": totals["windows"],
    })
}
