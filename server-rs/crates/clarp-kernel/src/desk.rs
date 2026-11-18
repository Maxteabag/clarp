//! The desk of a machine's account seats, as an app with its owner's full rights is told it:
//! the answer of `GET /hotseat/desk`.
//!
//! The owner's own tool (Hotseat) is asked three things at once: its whole desk
//! (`tui --snapshot`), its list of accounts (`list --json`) and the work it knows of
//! (`bridge work`). What they answered is made into one desk ([`built`]): every account with
//! its windows, its state, its clocks of signing in, the work that stopped and the work
//! that was done. That desk is kept ([`Desk`]) and served as it was read ([`Desk::served`]):
//! serving reads nothing of the tool, and says of every number how old it is. A window
//! whose time to begin anew has passed since it was read is served without its number, and
//! an account that was used up by such a window is no longer said to be.
//!
//! Reading costs: the tool asks the providers. So a read is one at a time, not before a
//! minute after the last that worked and not before ten seconds after any attempt
//! ([`Desk::next_read_at`]), and only when an app asks for one. A read of which the first
//! asking failed makes no desk: the desk before it stays, and is said to be stale. A read of
//! which the list or the work failed makes a desk of what answered, and says which did not.
//!
//! The list is also what the strict answer of `GET /hotseat/status` is made of
//! ([`crate::seats::Seats`]): a read here hands it over, as a read of the switcher would.
//!
//! What the tool's list says of a Clarp Host's agents, and of a Host's work that stopped, it
//! has from the Host it is bridged to. A Host that is not that one says its own word of its
//! agents in that place ([`of_this_host`]).
//!
//! This is the Python Host's `hotseat_desk.py` and `hotseat_desk_reading.py` (wire schema
//! `clarp.hotseat-desk/1`), by that Host's own answers (`tools/desk-vectors.py`).

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::python;
use crate::seats::Seats;

pub const SCHEMA: &str = "clarp.hotseat-desk/1";
/// How old a desk, and a window of it, may be before it is said to be stale.
pub const STALE_MS: i64 = 10 * 60 * 1000;
/// The tool is not read again before this long after a read that worked: no more often
/// than the Python Host's own switcher may be set to read it.
pub const READ_EVERY_MS: i64 = 60 * 1000;
/// Nor before this long after any attempt, a failed one included: a read that failed can be
/// tried again soon, and an app in a loop still cannot hammer a tool that is down.
pub const RETRY_AFTER_MS: i64 = 10 * 1000;
/// What the tool is asked, and how many seconds each asking may take.
pub const DESK_ASKED: (&[&str], u64) = (&["tui", "--snapshot"], 45);
pub const LIST_ASKED: (&[&str], u64) = (&["list", "--json"], 45);
pub const WORK_ASKED: (&[&str], u64) = (&["bridge", "work"], 20);
/// How an asking of the tool failed. An app is told which of these it was (by the Python
/// Host's codes), and nothing else of a failure is kept: not what the tool printed, and not
/// what it said of why.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Failed {
    /// It did not end within its time.
    #[serde(rename = "timeout")]
    TookTooLong,
    /// It could not be run, or it ended badly.
    #[serde(rename = "cli_failed")]
    DidNotRun,
    /// What it printed is no answer.
    #[serde(rename = "parse_failed")]
    NotReadable,
}
/// The windows that are an account's own: one of these at its limit and the account takes
/// no turn. A window of one model never is: the account still serves the other models.
const OF_THE_ACCOUNT: [&str; 3] = ["5h", "7d", "30d"];
const FAMILIES: [(&str, &str); 4] = [
    ("fable", "Fable"),
    ("opus", "Opus"),
    ("sonnet", "Sonnet"),
    ("haiku", "Haiku"),
];
/// No more of the work that was done is shown than this.
const WORK_SHOWN: usize = 40;
/// How long an agent whose last turn failed for a usage limit is listed as work that
/// stopped. One that nobody has touched for longer is at rest, and no work to continue.
pub const STOP_SHOWN_FOR_MS: i64 = 7 * 24 * 60 * 60 * 1000;

// ---- numbers, as the tool says them --------------------------------------------------------

/// A number that is one: no yes or no, and not without end.
fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Number(said) => said.as_f64().filter(|said| said.is_finite()),
        _ => None,
    }
}

/// A number to so many places, as Python's `round` gives it.
fn places(said: f64, count: usize) -> f64 {
    format!("{said:.count$}").parse().unwrap_or(said)
}

/// A used part of the whole, 0 to 1, to four places; nothing for what is no number, is below
/// nothing or far above the whole (`_ratio`). A missing number is never 0.
fn ratio(value: &Value) -> Option<f64> {
    let said = number(value).filter(|said| (0.0..=1.5).contains(said))?;
    Some(places(said.min(1.0), 4))
}

/// What is left of a whole of which so much is used.
fn left(used: Option<f64>) -> Value {
    used.map_or(Value::Null, |used| json!(places(1.0 - used, 4)))
}

/// Seconds since 1970 as milliseconds; nothing for a moment that is none
/// (`_epoch_ms_from_seconds`).
fn ms_of_seconds(value: &Value) -> Option<i64> {
    let said = number(value).filter(|said| *said > 0.0)?;
    Some((said * 1000.0).round_ties_even() as i64)
}

/// A moment in milliseconds; nothing for one that is none (`_int_ms`).
fn moment(value: &Value) -> Option<i64> {
    let whole = number(value)?.round_ties_even() as i64;
    (whole > 0).then_some(whole)
}

/// A count, as Python's `int(value or 0)` makes one of a number.
fn count(value: &Value) -> i64 {
    match value {
        Value::Bool(yes) => i64::from(*yes),
        other => number(other).map_or(0, |said| said.trunc() as i64),
    }
}

/// A whole number that is written as one (a yes counts as 1, as it does in Python).
fn whole(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(yes) => Some(i64::from(*yes)),
        Value::Number(said) => said.as_i64(),
        _ => None,
    }
}

/// The first of these that says something, as a text (`str(a or b or "")`).
fn said(values: &[&Value]) -> String {
    match values.iter().find(|value| python::truthy(value)) {
        Some(Value::String(text)) => text.clone(),
        Some(other) => python::repr(other),
        None => String::new(),
    }
}

fn text_of(value: &Value) -> Option<&str> {
    value.as_str().filter(|text| !text.is_empty())
}

fn items(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}

// ---- what kind of window a label is ----------------------------------------------------------

fn pair(kind: &str, short: &str) -> Option<(String, String)> {
    Some((kind.to_string(), short.to_string()))
}

/// Claude: "All models · 5-hour" and "All models · weekly" are the account's own windows;
/// "<Model> · weekly" is that model's (`_claude_window_meta`).
fn claude_window(raw: &str) -> Option<(String, String)> {
    let lower = raw.to_lowercase();
    let five_hours = lower.contains("5-hour") || lower.contains("5h");
    let every_model = lower.starts_with("all models");
    if (every_model || !raw.contains('·')) && five_hours {
        return pair("5h", "5h");
    }
    if every_model && lower.contains("weekly") {
        return pair("7d", "Weekly");
    }
    let (family, title) = FAMILIES.iter().find(|(family, _)| lower.contains(family))?;
    if five_hours {
        pair(&format!("model:{family}:5h"), &format!("{title} 5h"))
    } else {
        pair(&format!("model:{family}"), title)
    }
}

/// Codex: the tool labels a window "<limit> [tier] <span>"; the account's own limit carries
/// the tool's name (`_codex_window_meta`).
fn codex_window(tool: &str, raw: &str) -> Option<(String, String)> {
    let lower = raw.to_lowercase();
    let five_hours = lower.contains("5-hour") || lower.contains("5h");
    if lower.contains("spark") {
        return if five_hours {
            pair("model:spark:5h", "Spark 5h")
        } else {
            pair("model:spark", "Spark wk")
        };
    }
    let spaced = lower.replace('·', " ");
    let words: Vec<&str> = spaced.split_whitespace().collect();
    if words.first() != Some(&tool) && words.len() != 1 {
        return None;
    }
    if five_hours {
        pair("5h", "5h")
    } else if lower.contains("30-day") {
        pair("30d", "30-day")
    } else if lower.contains("weekly") {
        pair("7d", "Weekly")
    } else {
        None
    }
}

/// The kind of a window and its short name, from the label the tool gives it
/// (`_window_meta`). A window of the account itself gets one of three kinds; a window of one
/// model never does, and the kind is what keeps the two apart. A label of a tool that has no
/// reading here, and one that is none of a tool's own, is a window by its own name.
pub fn window_kind(tool: &str, label: &str) -> (String, String) {
    let raw = python::strip(label);
    let known = match tool {
        "claude" => claude_window(raw),
        "codex" => codex_window(tool, raw),
        _ => None,
    };
    known.unwrap_or_else(|| {
        let short = python::first(raw, 24);
        let lower = raw.to_lowercase();
        (
            format!("window:{}", python::first(&lower, 24)),
            if short.is_empty() { "Window" } else { short }.to_string(),
        )
    })
}

/// The command a person types in a terminal to take a session of a tool up again, for an app
/// to show or copy; none for a tool that has none this Host knows of: nobody is offered
/// another tool's (`_resume_command`).
pub fn resume_command(tool: &str, session: &str) -> Option<String> {
    match python::strip(tool).to_lowercase().as_str() {
        "claude" => Some(format!("claude --resume {session}")),
        "codex" => Some(format!("codex resume {session}")),
        _ => None,
    }
}

// ---- what the list adds to an account --------------------------------------------------------

/// What the list adds to an account when it says nothing of it (`no_extras`).
fn nothing_added() -> Map<String, Value> {
    let nothing = json!({
        "email": "", "workspace": "", "plan": "", "plan_code": null, "rate_limit_tier": "",
        "error_code": null, "blocked": false, "extra_usage": false, "blocked_models": [],
        "access_expires_at": null, "access_hours_left": null, "refresh_expires_at": null,
        "signin_days_left": null, "signin_due_soon": null, "token_expires_at": null,
        "token_hours_left": null, "api_key_present": null, "extra_usage_enabled": null,
        "extra_usage_reason": "",
    });
    nothing.as_object().cloned().unwrap_or_default()
}

fn rounded(value: &Value, count: usize) -> Value {
    number(value).map_or(Value::Null, |said| json!(places(said, count)))
}

/// A row of an account of Claude (`_claude_extras`).
fn claude_adds(row: &Value) -> Value {
    let usage = &row["usage"];
    let usage = if usage.is_object() {
        usage
    } else {
        &Value::Null
    };
    let models: Vec<&Value> = items(&usage["blocked_models"])
        .iter()
        .filter(|model| {
            model
                .as_str()
                .is_some_and(|model| !python::strip(model).is_empty())
        })
        .collect();
    let enabled = usage.get("extra_usage_enabled");
    json!({
        "email": row["email"], "workspace": row["org"], "plan": row["plan_label"],
        "plan_code": row["plan"], "rate_limit_tier": row["rate_limit_tier"],
        "error_code": row["error_code"].as_str(),
        "extra_usage": usage["extra_usage_enabled"] == true,
        "blocked_models": models,
        "access_expires_at": moment(&row["access_expires_at"]),
        "access_hours_left": rounded(&row["access_hours_left"], 2),
        "refresh_expires_at": moment(&row["refresh_expires_at"]),
        "signin_days_left": rounded(&row["signin_days_left"], 1),
        "signin_due_soon": python::truthy(&row["signin_due_soon"]),
        "extra_usage_enabled": enabled.map(python::truthy),
        "extra_usage_reason": said(&[&usage["extra_usage_reason"]]),
    })
}

/// A row of an account of Codex (`_codex_extras`).
fn codex_adds(row: &Value) -> Value {
    let usage = &row["usage"];
    json!({
        "email": row["email"], "workspace": row["account_id"], "plan": row["plan"],
        "plan_code": row["plan"],
        "blocked": usage.is_object() && python::truthy(&usage["blocked"]),
        "token_expires_at": ms_of_seconds(&row["token_expires_at"]),
        "token_hours_left": rounded(&row["token_hours_left"], 2),
        "api_key_present": row.get("api_key_present").map(python::truthy),
    })
}

/// The rows of the list by the alias each is of; of two of one alias, the later.
fn by_alias(rows: &Value) -> Map<String, Value> {
    let mut found = Map::new();
    for row in items(rows) {
        if let (true, Some(alias)) = (row.is_object(), row["alias"].as_str()) {
            found.insert(alias.to_string(), row.clone());
        }
    }
    found
}

// ---- an account ------------------------------------------------------------------------------

fn at_its_limit(windows: &[Value]) -> bool {
    windows.iter().any(|window| {
        let kind = window["kind"].as_str().unwrap_or("");
        OF_THE_ACCOUNT.contains(&kind) && number(&window["used_ratio"]).unwrap_or(0.0) >= 1.0
    })
}

/// The state of the account, and the code that goes with it (`_classify_account_state`).
/// `exhausted`: the account cannot take a turn: the tool says it is limited, or one of its
/// own windows is at its limit and no extra usage covers it.
fn state_of(
    error: &str,
    code: Option<&str>,
    limited: bool,
    windows: &[Value],
    extra_usage: bool,
) -> (&'static str, Option<String>) {
    let lower = error.to_lowercase();
    let login = ["expired", "revoked", "sign-in"]
        .iter()
        .any(|word| lower.contains(word));
    if code == Some("auth_error") || (lower.contains("token") && login) {
        return ("auth_error", Some("auth_error".to_string()));
    }
    if !error.is_empty() {
        let code = code.filter(|code| !code.is_empty());
        return ("error", Some(code.unwrap_or("provider_error").to_string()));
    }
    if limited || (!extra_usage && at_its_limit(windows)) {
        return ("exhausted", Some("exhausted".to_string()));
    }
    if windows.iter().any(|window| !window["used_ratio"].is_null()) {
        return ("ok", None);
    }
    ("unknown", None)
}

/// One account of the desk: `raw` as the tool's desk reports it, `row` what its list has for
/// the same tool and alias (`_account_row`).
fn account(raw: &Value, row: Option<&Value>, agents: &Value) -> Option<Value> {
    let tool = python::strip(&said(&[&raw["provider"]])).to_lowercase();
    let alias = python::strip(&said(&[&raw["alias"]])).to_string();
    if tool.is_empty() || alias.is_empty() {
        return None;
    }
    // When the tool checked this account, or nothing where it gave no time (a check that
    // failed, an account it never verified). Never the time of the read: that would date a
    // number nobody dated.
    let checked = ms_of_seconds(&raw["checked_at"]);
    let error = python::strip(&said(&[&raw["error"]])).to_string();
    let kept_reading = python::truthy(&raw["cached"]);
    let mut windows = Vec::new();
    for window in items(&raw["windows"]).iter().filter(|one| one.is_object()) {
        let label = python::strip(&said(&[&window["label"]])).to_string();
        let (kind, short) = window_kind(&tool, &label);
        let used = if error.is_empty() {
            ratio(&window["used"])
        } else {
            None
        };
        windows.push(json!({
            "kind": kind, "label": label, "short_label": short, "used_ratio": used,
            "remaining_ratio": left(used), "resets_at": ms_of_seconds(&window["reset"]),
            "observed_at": checked, "stale": kept_reading,
        }));
    }
    let mut adds = nothing_added();
    let nothing = json!({});
    let row = row.unwrap_or(&nothing);
    let added = match tool.as_str() {
        "claude" => claude_adds(row),
        "codex" => codex_adds(row),
        _ => Value::Null,
    };
    for (key, value) in added.as_object().into_iter().flatten() {
        adds.insert(key.clone(), value.clone());
    }
    let adds = Value::Object(adds);
    let limited = python::truthy(&raw["limited"]) || python::truthy(&adds["blocked"]);
    let (state, code) = state_of(
        &error,
        adds["error_code"].as_str(),
        limited,
        &windows,
        python::truthy(&adds["extra_usage"]),
    );
    let aliases: Vec<&Value> = match &raw["aliases"] {
        Value::Array(named) if !named.is_empty() => {
            named.iter().filter(|alias| alias.is_string()).collect()
        }
        _ => Vec::new(),
    };
    let aliases = if python::truthy(&raw["aliases"]) {
        json!(aliases)
    } else {
        json!([alias])
    };
    let notes: Vec<&Value> = items(&raw["profile_notes"])
        .iter()
        .filter(|note| {
            note.as_str()
                .is_some_and(|note| !python::strip(note).is_empty())
        })
        .collect();
    let credits = match &raw["reset_credits"] {
        Value::Number(credits) if !credits.is_f64() => raw["reset_credits"].clone(),
        _ => Value::Null,
    };
    let plan_code = said(&[&adds["plan_code"]]).to_lowercase();
    let plan_code = (!plan_code.is_empty()).then_some(plan_code);
    let sessions = |of: &str| count(&agents[of][alias.as_str()]);
    Some(json!({
        "provider": tool,
        "alias": alias,
        "display_alias": said(&[&raw["display_alias"], &json!(alias)]),
        "aliases": aliases,
        "email": said(&[&raw["email"], &adds["email"]]),
        "workspace": said(&[&raw["workspace"], &adds["workspace"]]),
        "plan": said(&[&raw["plan"], &adds["plan"]]),
        "plan_code": plan_code,
        "rate_limit_tier": said(&[&raw["rate_limit_tier"], &adds["rate_limit_tier"]]),
        "active": python::truthy(&raw["active"]),
        "saved": raw.get("saved").is_none_or(python::truthy),
        "can_switch": python::truthy(&raw["can_switch"]),
        "can_launch": python::truthy(&raw["can_launch"]),
        "limited": limited,
        "cached": kept_reading,
        "state": state,
        "error_code": code,
        "error": error,
        "warning": said(&[&raw["warning"]]),
        "profile_notes": notes,
        "no_reset_reason": python::strip(&said(&[&raw["no_reset_reason"]])),
        "checked_at": checked,
        "last_good_checked_at": if error.is_empty() { checked } else { None },
        "reset_credits": credits,
        "reset_credits_error": said(&[&raw["reset_credits_error"]]),
        "access_expires_at": adds["access_expires_at"],
        "access_hours_left": adds["access_hours_left"],
        "refresh_expires_at": adds["refresh_expires_at"],
        "signin_days_left": adds["signin_days_left"],
        "signin_due_soon": adds["signin_due_soon"],
        "token_expires_at": adds["token_expires_at"],
        "token_hours_left": adds["token_hours_left"],
        "api_key_present": adds["api_key_present"],
        "blocked_models": adds["blocked_models"],
        "extra_usage_enabled": adds["extra_usage_enabled"],
        "extra_usage_reason": adds["extra_usage_reason"],
        "live_sessions": sessions("live_by_account"),
        "would_use_sessions": sessions("would_use_by_account"),
        "windows": windows,
    }))
}

// ---- the agents and the work -----------------------------------------------------------------

/// What is said of the agents where the list says nothing of them (`_no_clarp_agents`).
pub fn no_agents() -> Value {
    json!({
        "total": 0, "live": 0, "claude_backed": 0,
        "by_backend": {}, "live_by_account": {}, "would_use_by_account": {},
    })
}

fn counts(named: &Value) -> Map<String, Value> {
    let named = named.as_object().into_iter().flatten();
    named
        .filter_map(|(name, said)| Some((name.clone(), json!(whole(said)?))))
        .collect()
}

/// One agent of this Host, as far as the desk says something of it.
#[derive(Debug, Clone, Copy, Default)]
pub struct Agent<'a> {
    /// Its name in every request.
    pub session: &'a str,
    /// What a person calls it.
    pub name: &'a str,
    /// The tool it runs with, by the tool's name.
    pub tool: &'a str,
    /// The model it runs on; empty: the tool's own choice.
    pub model: &'a str,
    /// Where it works.
    pub cwd: &'a str,
    /// Whether a turn of it runs now.
    pub working: bool,
    /// Its tool's own session, once it has one.
    pub tool_session: Option<&'a str>,
    /// Why its work stopped and when, where it did and has not gone on since.
    pub stopped: Option<(Cause, i64)>,
    /// How many of its turns wait.
    pub waiting: usize,
}

/// Why an agent's work stopped.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Cause {
    /// Its last turn failed for a usage limit, and none has run since.
    UsageLimit,
    /// A turn of it waits for an account that serves it.
    WaitsForAnAccount,
    /// Its queue is held, with turns waiting in it.
    QueueHeld,
}

impl Cause {
    /// As the owner's tool names it.
    pub fn word(self) -> &'static str {
        match self {
            Self::UsageLimit => "usage_limit",
            Self::WaitsForAnAccount => "waiting_for_account",
            Self::QueueHeld => "queue_paused",
        }
    }
}

/// Whether work that stopped for this cause could go on, by what the reading says of the
/// account its tool is on: the shown alias of that account and its state, where one is in
/// use. A held queue is let go, not continued.
fn readiness(cause: Cause, tool: &str, in_use: Option<(&str, &str)>) -> Value {
    let (ready, hard, warning) = match (cause, in_use) {
        (Cause::QueueHeld, _) => {
            let held = "queue is paused; a prompt would only queue".to_string();
            (false, Some(held), None)
        }
        (_, None) => (false, Some(format!("no account of {tool} is in use")), None),
        (_, Some((alias, "ok"))) => (true, None, Some(format!("{alias} could serve it"))),
        (_, Some((alias, "unknown"))) => {
            let unknown = format!("what is left of {alias} is not known");
            (true, None, Some(unknown))
        }
        (_, Some((alias, "exhausted"))) => {
            (false, Some(format!("{alias} has no quota left")), None)
        }
        (_, Some((alias, "auth_error"))) => (false, Some(format!("{alias} is signed out")), None),
        (_, Some((alias, _))) => (false, Some(format!("{alias} could not be read")), None),
    };
    json!({ "ready": ready, "hard": hard, "warning": warning })
}

fn one_more(counts: &mut Map<String, Value>, of: &str) {
    let so_far = counts.get(of).and_then(Value::as_i64).unwrap_or(0);
    counts.insert(of.to_string(), json!(so_far + 1));
}

/// This Host's own word of its agents, in the place and in the shape of what the tool's
/// list says of a Clarp Host's (its `extensions.clarp`).
///
/// The tool has what it says there from the Host it is bridged to: on a machine with
/// another Host, that Host's agents and that Host's stopped work, which this Host could
/// neither vouch for nor continue. So the agents are this Host's, counted: each uses the
/// account its tool is on, which is the one the tool's desk says is in use. The work that
/// stopped is this Host's own first: each agent whose work stopped, with why and when, and
/// whether it could go on by what this reading says of the account its tool is on. Of the
/// work the tool says stopped, a Host's own is left to that Host; a session of the machine
/// that is no Host's stays, unless it is the session of one of this Host's agents: that is
/// no session to take up in a terminal. A stop for a usage limit is listed for a week
/// ([`STOP_SHOWN_FOR_MS`]); a turn that waits and a queue that is held are listed while
/// they do and are.
pub fn of_this_host(
    desk: &Value,
    list: Option<&Value>,
    agents: &[Agent<'_>],
    now_ms: i64,
) -> Value {
    let nothing = Value::Null;
    let listed_rows = list.unwrap_or(&nothing);
    // The account each tool is on, as the reading has it: its alias, the alias it is shown
    // by, and its state.
    let in_use = |tool: &str| {
        let rows = match tool {
            "claude" => by_alias(&listed_rows["accounts"]),
            "codex" => by_alias(&listed_rows["codex"]["accounts"]),
            _ => Map::new(),
        };
        items(&desk["accounts"]).iter().find_map(|raw| {
            let of = python::strip(&said(&[&raw["provider"]])).to_lowercase();
            let alias = python::strip(&said(&[&raw["alias"]])).to_string();
            if of != tool || !python::truthy(&raw["active"]) || alias.is_empty() {
                return None;
            }
            let read = account(raw, rows.get(&alias), &no_agents())?;
            let shown = said(&[&read["display_alias"]]);
            Some((alias, shown, said(&[&read["state"]])))
        })
    };
    let (mut by_tool, mut at_work) = (Map::new(), Map::new());
    let mut listed = Vec::new();
    let mut stopped: Vec<Value> = Vec::new();
    for agent in agents {
        one_more(&mut by_tool, agent.tool);
        let account = in_use(agent.tool);
        let alias = account.as_ref().map(|(alias, _, _)| alias.as_str());
        if let (true, Some(alias)) = (agent.working, alias) {
            one_more(&mut at_work, alias);
        }
        listed.push(json!({ "session": agent.session, "would_use": alias }));
        let long_ago = |at_ms: i64| now_ms - at_ms > STOP_SHOWN_FOR_MS;
        let its_stop = agent
            .stopped
            .filter(|(cause, at_ms)| *cause != Cause::UsageLimit || !long_ago(*at_ms));
        if let Some((cause, at_ms)) = its_stop {
            let of_account = account
                .as_ref()
                .map(|(_, shown, state)| (shown.as_str(), state.as_str()));
            stopped.push(json!({
                "kind": "clarp",
                "cause": cause.word(),
                "pending": agent.waiting,
                "id": agent.session,
                "name": agent.name,
                "backend": agent.tool,
                "model": agent.model,
                "account": alias,
                "cwd": agent.cwd,
                "stopped_at": at_ms as f64 / 1000.0,
                "readiness": readiness(cause, agent.tool, of_account),
            }));
        }
    }
    let said_stopped = list.map_or(&nothing, |list| &list["extensions"]["clarp"]["stopped"]);
    let of_an_agent = |item: &Value| {
        let id = said(&[&item["id"]]);
        let mut sessions = agents.iter().filter_map(|agent| agent.tool_session);
        sessions.any(|session| session == python::strip(&id))
    };
    let of_nobody = items(said_stopped)
        .iter()
        .filter(|item| item["kind"] == "native" && !of_an_agent(item));
    stopped.extend(of_nobody.cloned());
    let counted =
        |which: &dyn Fn(&Agent<'_>) -> bool| agents.iter().filter(|one| which(one)).count();
    json!({
        "agents": {
            "total": agents.len(),
            "live": counted(&|agent| agent.working),
            "claude_backed": counted(&|agent| agent.tool == "claude"),
            "by_backend": by_tool,
            "live_by_account": at_work,
            "agents": listed,
        },
        "stopped": stopped,
    })
}

/// The agents, counted, and the work that stopped, as the part of the list that is about a
/// Clarp Host says them (`_clarp_block`).
fn agents_and_stopped(of_the_host: Option<&Value>) -> (Value, Vec<Value>) {
    let nothing = Value::Null;
    let of_the_host = of_the_host.unwrap_or(&nothing);
    if !of_the_host.is_object() {
        return (no_agents(), Vec::new());
    }
    let mut agents = no_agents();
    let block = &of_the_host["agents"];
    if block.is_object() {
        let mut would_use = Map::new();
        for agent in items(&block["agents"]) {
            if let Some(account) = text_of(&agent["would_use"]) {
                let so_far = would_use.get(account).and_then(Value::as_i64).unwrap_or(0);
                would_use.insert(account.to_string(), json!(so_far + 1));
            }
        }
        agents = json!({
            "total": count(&block["total"]),
            "live": count(&block["live"]),
            "claude_backed": count(&block["claude_backed"]),
            "by_backend": counts(&block["by_backend"]),
            "live_by_account": counts(&block["live_by_account"]),
            "would_use_by_account": would_use,
        });
    }
    let stopped = items(&of_the_host["stopped"]).iter();
    (
        agents,
        stopped.filter(|one| one.is_object()).cloned().collect(),
    )
}

/// The work that stopped, as it is shown (`_stopped_rows`). What the tool did not say stays
/// unknown: a row of unknown kind cannot be continued, and an unknown cause is not shown as
/// a usage limit.
fn stopped_work(stopped: &[Value]) -> Vec<Value> {
    let mut shown = Vec::new();
    for item in stopped {
        let kind = item["kind"]
            .as_str()
            .filter(|kind| ["clarp", "native"].contains(kind));
        let cause = item["cause"].as_str().map_or("", python::strip);
        let id = python::strip(&said(&[&item["id"]])).to_string();
        if id.is_empty() {
            continue;
        }
        let tool = said(&[&item["backend"]]);
        let readiness = item["readiness"].as_object().map(|ready| {
            let word = |of: &str| ready.get(of).and_then(text_of);
            json!({
                "ready": ready.get("ready").is_some_and(python::truthy),
                "hard": word("hard"), "warning": word("warning"),
            })
        });
        let command = (kind == Some("native"))
            .then(|| resume_command(&tool, &id))
            .flatten();
        shown.push(json!({
            "kind": kind,
            "cause": (!cause.is_empty()).then_some(cause),
            "pending": count(&item["pending"]),
            "id": id,
            "name": said(&[&item["name"], &json!(id)]),
            "backend": tool,
            "model": said(&[&item["model"]]),
            "account": said(&[&item["account"]]),
            "cwd": said(&[&item["cwd"]]),
            "last_message": said(&[&item["last_message"]]),
            "stopped_at": ms_of_seconds(&item["stopped_at"]),
            "readiness": readiness,
            "resume_command": command,
        }));
    }
    shown
}

fn recent_work(work: Option<&Value>) -> Vec<Value> {
    let nothing = Value::Null;
    let listed = items(work.map_or(&nothing, |work| &work["items"])).iter();
    let mut shown = Vec::new();
    for item in listed.take(WORK_SHOWN).filter(|one| one.is_object()) {
        let id = python::strip(&said(&[&item["id"]])).to_string();
        if id.is_empty() {
            continue;
        }
        let tool = said(&[&item["provider"]]);
        let can_resume = python::truthy(&item["can_resume"]);
        let live = python::truthy(&item["live"]);
        let command = (can_resume && !live)
            .then(|| resume_command(&tool, &id))
            .flatten();
        let state = said(&[&item["state"], &json!("unknown")]);
        shown.push(json!({
            "id": id,
            "provider": tool,
            "title": said(&[&item["title"]]),
            "state": state,
            "updated_at": ms_of_seconds(&item["updated"]),
            "cwd": said(&[&item["cwd"]]),
            "live": live,
            "can_resume": can_resume,
            "can_reboot": python::truthy(&item["can_reboot"]),
            "reason": said(&[&item["reason"]]),
            "resume_command": command,
        }));
    }
    shown
}

/// The desk, made of what the tool answered to its three askings (`_build_desk_snapshot`):
/// its whole desk, and its list and its work where those answered. `of_the_host`: what is
/// said of a Clarp Host's agents and stopped work.
fn built(
    desk: &Value,
    list: Option<&Value>,
    of_the_host: Option<&Value>,
    work: Option<&Value>,
    observed_at: i64,
) -> Value {
    let nothing = Value::Null;
    let listed = list.unwrap_or(&nothing);
    let rows = [
        ("claude", by_alias(&listed["accounts"])),
        ("codex", by_alias(&listed["codex"]["accounts"])),
    ];
    let (agents, stopped) = agents_and_stopped(of_the_host);
    let mut accounts = Vec::new();
    for raw in items(&desk["accounts"])
        .iter()
        .filter(|one| one.is_object())
    {
        let tool = python::strip(&said(&[&raw["provider"]])).to_lowercase();
        let alias = python::strip(&said(&[&raw["alias"]])).to_string();
        let of_tool = rows.iter().find(|(named, _)| *named == tool);
        let row = of_tool.and_then(|(_, rows)| rows.get(&alias));
        accounts.extend(account(raw, row, &agents));
    }
    let errors: Vec<&Value> = items(&desk["errors"])
        .iter()
        .filter(|error| {
            error
                .as_str()
                .is_some_and(|error| !python::strip(error).is_empty())
        })
        .collect();
    let sessions = match &desk["sessions"] {
        Value::Number(sessions) if !sessions.is_f64() => desk["sessions"].clone(),
        _ => json!(0),
    };
    json!({
        "v": 1,
        "observed_at": observed_at,
        "sessions": sessions,
        "errors": errors,
        "clarp_agents": agents,
        "accounts": accounts,
        "stopped_work": stopped_work(&stopped),
        "recent_work": recent_work(work),
    })
}

// ---- what the tool answered ------------------------------------------------------------------

/// What one asking of the tool came to: what it printed, read; or how it failed.
pub type Answer = Result<Value, Failed>;

/// Reads what the tool printed for one asking, and how it ended (`_run_json`). A tool that
/// printed nothing said an empty thing.
pub fn answer(exit: Option<i32>, printed: &str) -> Answer {
    if exit != Some(0) {
        return Err(Failed::DidNotRun);
    }
    let printed = if printed.is_empty() { "{}" } else { printed };
    match serde_json::from_str::<Value>(printed) {
        Ok(read) if read.is_object() => Ok(read),
        _ => Err(Failed::NotReadable),
    }
}

/// What the tool answered to its three askings.
pub struct Answers {
    pub desk: Answer,
    pub list: Answer,
    pub work: Answer,
    /// What is said of a Clarp Host's agents and stopped work in the place of what the
    /// list says of them ([`of_this_host`]). `None`: what the list says, as the Python Host
    /// takes it.
    pub agents: Option<Value>,
}

/// What became of a live read that was asked for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// A read ran and made a desk.
    Read,
    /// A read ran and made none.
    Failed,
    /// None was allowed yet, and nothing ran.
    Held,
}

impl Outcome {
    /// As the Python Host's desk names it.
    pub fn word(self) -> &'static str {
        match self {
            Self::Read => "ok",
            Self::Failed => "failed",
            Self::Held => "throttled",
        }
    }
}

/// An asking of an app, and what the Host knows at that moment.
pub struct Asked {
    pub served_at: i64,
    /// Whether the owner's tool is to be found on this machine.
    pub installed: bool,
    /// Whether a live read is under way.
    pub reading: bool,
    /// What became of the live read this asking asked for, where it asked for one.
    pub outcome: Option<Outcome>,
    /// When this Host, since it started, last began a live read; 0 where it began none.
    pub last_attempt_ms: i64,
}

// ---- the desk that is kept -------------------------------------------------------------------

/// What is kept of the desk: the desk the last live read that worked made, and how the last
/// live read failed.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Desk {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    read: Option<Value>,
    /// How the last read failed; nothing when it worked.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    error: Option<Failed>,
}

impl Desk {
    /// When the last full live read that worked was made.
    pub fn live_read_at(&self) -> Option<i64> {
        moment(&self.read.as_ref()?["live_read_at"])
    }

    /// The work that stopped, as the kept desk lists it; nothing where no desk was read.
    pub fn stopped_work(&self) -> Option<&Value> {
        self.read.as_ref().map(|read| &read["stopped_work"])
    }

    /// When the kept desk was read.
    pub fn observed_at(&self) -> Option<i64> {
        moment(&self.read.as_ref()?["observed_at"])
    }

    /// When the next live read may begin (`_next_allowed`). The whole interval counts from
    /// the last read that worked, which is kept, so also across a start of the Host; any
    /// attempt of this Host since it started holds the next off for the short wait.
    pub fn next_read_at(&self, last_attempt_ms: i64) -> i64 {
        let after = |since: i64, wait: i64| if since == 0 { 0 } else { since + wait };
        let read = self.live_read_at().unwrap_or(0);
        after(last_attempt_ms, RETRY_AFTER_MS).max(after(read, READ_EVERY_MS))
    }

    /// A live read ended, having begun at `began_ms`: the tool's answers are made into the
    /// desk, and its list is handed to the seats' status as a read of the switcher would
    /// be. Whether it made a desk: where the tool's own desk did not answer, none is made,
    /// the desk before stays, and how it failed is kept.
    pub fn read(&mut self, seats: &mut Seats, answers: Answers, began_ms: i64) -> bool {
        let desk = match answers.desk {
            Ok(desk) => desk,
            Err(how) => {
                self.error = Some(how);
                return false;
            }
        };
        let (list, work) = (answers.list.ok(), answers.work.ok());
        if let Some(list) = &list {
            seats.read("claude", list, began_ms);
            if list["codex"].is_object() {
                seats.read("codex", &list["codex"], began_ms);
            }
        }
        let of_the_host = match (&answers.agents, &list) {
            (Some(own), _) => Some(own),
            (None, Some(list)) => Some(&list["extensions"]["clarp"]),
            (None, None) => None,
        };
        let mut made = built(&desk, list.as_ref(), of_the_host, work.as_ref(), began_ms);
        // Which of the three askings this desk is made of answered. Without the list the
        // accounts lack their clocks of signing in, their blocked models and their typed
        // errors and there is no stopped work; without the work there is no recent work.
        let answered = |one: bool| if one { "ok" } else { "failed" };
        made["sources"] = json!({
            "tui": "ok", "list": answered(list.is_some()), "work": answered(work.is_some()),
        });
        made["live_read_at"] = json!(began_ms);
        // An account whose check failed this time was last known good when the desk before
        // says it was (`_carry_last_good`).
        let nothing = Value::Null;
        let before = self
            .read
            .as_ref()
            .map_or(&nothing, |read| &read["accounts"]);
        for account in made["accounts"].as_array_mut().into_iter().flatten() {
            if account["last_good_checked_at"].is_null() {
                let same = |known: &&Value| {
                    known["provider"] == account["provider"] && known["alias"] == account["alias"]
                };
                let known = items(before).iter().rfind(same);
                account["last_good_checked_at"] =
                    json!(known.and_then(|known| moment(&known["last_good_checked_at"])));
            }
        }
        self.read = Some(made);
        self.error = None;
        true
    }

    /// The desk as it is kept, said at this moment (`HotseatDesk.payload`). `status`: what
    /// `GET /hotseat/status` says at the same moment ([`Seats::served`]): its switcher, and,
    /// where no desk was ever read, its accounts.
    pub fn served(&self, status: &Value, asked: &Asked) -> Value {
        let now = asked.served_at;
        let error = self.error;
        let sources = self.read.as_ref().map(|read| &read["sources"]);
        let sources = sources.filter(|sources| sources.is_object());
        let partial = sources.is_none_or(|sources| {
            ["tui", "list", "work"]
                .iter()
                .any(|asking| sources[*asking] != "ok")
        });
        let mut said = match &self.read {
            Some(read) => {
                let observed_at = moment(&read["observed_at"]);
                let stale = observed_at.is_none_or(|at| now - at > STALE_MS) || error.is_some();
                let agents = match &read["clarp_agents"] {
                    Value::Object(agents) if !agents.is_empty() => read["clarp_agents"].clone(),
                    _ => no_agents(),
                };
                json!({
                    "observed_at": observed_at,
                    "stale": stale,
                    "accounts": served_accounts(items(&read["accounts"]), now, stale),
                    "sessions": count(&read["sessions"]),
                    "errors": items(&read["errors"]),
                    "clarp_agents": agents,
                    "stopped_work": items(&read["stopped_work"]),
                    "recent_work": items(&read["recent_work"]),
                })
            }
            None => {
                let (accounts, observed_at) = of_the_status(status);
                json!({
                    "observed_at": observed_at, "stale": true, "accounts": accounts,
                    "sessions": 0, "errors": [], "clarp_agents": no_agents(),
                    "stopped_work": [], "recent_work": [],
                })
            }
        };
        let mut switcher = match &status["switcher"] {
            Value::Object(switcher) => switcher.clone(),
            _ => Map::new(),
        };
        switcher.insert("actions".to_string(), json!(items(&status["actions"])));
        let more = json!({
            "schema": SCHEMA,
            "served_at": now,
            // When the last full live read was made; nothing when none is kept.
            "live_read_at": self.live_read_at(),
            "refreshing": asked.reading,
            // True only when the live read THIS asking asked for ran and all three of the
            // tool's askings answered: a complete, new desk.
            "refreshed": asked.outcome == Some(Outcome::Read) && !partial,
            "refresh_throttled": asked.outcome == Some(Outcome::Held),
            "next_refresh_at": self.next_read_at(asked.last_attempt_ms),
            "sources": sources,
            "partial": partial,
            "installed": asked.installed,
            "error": error,
            "switcher": switcher,
            "switches": [],
        });
        if let (Some(said), Some(more)) = (said.as_object_mut(), more.as_object()) {
            said.extend(more.clone());
        }
        said
    }
}

/// The accounts of a kept desk as they are served at a moment (`_serve_accounts`). A window
/// also ages on its own, and one nobody can date is not served as current. A window whose
/// time to begin anew has passed since it was read is served without its number; an account
/// that was used up, of which such a window has passed and none of its own is at its limit
/// any more, is no longer said to be used up.
fn served_accounts(kept: &[Value], served_at: i64, desk_stale: bool) -> Vec<Value> {
    let mut served = Vec::new();
    for account in kept.iter().filter(|one| one.is_object()) {
        let mut account = account.clone();
        let never_anew = python::truthy(&account["no_reset_reason"]);
        let mut windows = Vec::new();
        let mut passed = false;
        for window in items(&account["windows"])
            .iter()
            .filter(|one| one.is_object())
        {
            let mut window = window.clone();
            let mut used = ratio(&window["used_ratio"]);
            let anew_at = moment(&window["resets_at"]);
            let observed = moment(&window["observed_at"]);
            let mut stale = python::truthy(&window["stale"])
                || desk_stale
                || observed.is_none_or(|at| served_at - at > STALE_MS);
            let began_anew = anew_at
                .is_some_and(|anew| anew <= served_at && observed.is_none_or(|at| at < anew));
            if !never_anew && began_anew {
                used = None;
                stale = true;
                passed = true;
            }
            window["used_ratio"] = json!(used);
            window["remaining_ratio"] = left(used);
            window["resets_at"] = json!(anew_at);
            window["stale"] = json!(stale);
            windows.push(window);
        }
        if account["state"] == "exhausted" && passed && !at_its_limit(&windows) {
            account["state"] = json!("unknown");
            account["error_code"] = Value::Null;
            account["limited"] = json!(false);
        }
        account["windows"] = json!(windows);
        served.push(account);
    }
    served
}

/// The accounts of a desk that was never read, from what the seats' status says of them,
/// and when the newest of that was read (`_fallback_accounts_from_status`).
fn of_the_status(status: &Value) -> (Vec<Value>, Option<i64>) {
    let mut accounts = Vec::new();
    let mut newest: Option<i64> = None;
    for of_tool in items(&status["providers"])
        .iter()
        .filter(|one| one.is_object())
    {
        let tool = said(&[&of_tool["provider"]]);
        newest = newest.max(moment(&of_tool["observed_at"]));
        for account in items(&of_tool["accounts"])
            .iter()
            .filter(|one| one.is_object())
        {
            let alias = said(&[&account["alias"], &json!("account")]);
            let mut windows = Vec::new();
            for window in items(&account["windows"])
                .iter()
                .filter(|one| one.is_object())
            {
                let kind = said(&[&window["kind"]]);
                let label = said(&[&window["label"], &json!(kind)]);
                windows.push(json!({
                    "kind": kind,
                    "label": label,
                    "short_label": window_kind(&tool, &label).1,
                    "used_ratio": ratio(&window["used_ratio"]),
                    "remaining_ratio": ratio(&window["remaining_ratio"]),
                    "resets_at": moment(&window["resets_at"]),
                    "observed_at": moment(&window["observed_at"]),
                    "stale": python::truthy(&window["stale"]),
                }));
            }
            let plan_code = said(&[&account["plan"]]).to_lowercase();
            let plan_code = (!plan_code.is_empty()).then_some(plan_code);
            let good = moment(&account["last_good_observed_at"]);
            accounts.push(json!({
                "provider": tool, "alias": alias, "display_alias": alias, "aliases": [alias],
                "email": "", "workspace": "",
                "plan": said(&[&account["label"], &account["plan"]]),
                "plan_code": plan_code,
                "rate_limit_tier": "",
                "active": python::truthy(&account["active_default"]),
                "saved": true, "can_switch": false, "can_launch": false,
                "limited": account["state"] == "exhausted",
                "cached": true,
                "state": said(&[&account["state"], &json!("unknown")]),
                "error_code": account["error_code"],
                "error": "", "warning": "", "profile_notes": [], "no_reset_reason": "",
                "checked_at": good, "last_good_checked_at": good,
                "reset_credits": null, "reset_credits_error": "",
                "access_expires_at": null, "access_hours_left": null, "refresh_expires_at": null,
                "signin_days_left": null, "signin_due_soon": null, "token_expires_at": null,
                "token_hours_left": null, "api_key_present": null, "blocked_models": [],
                "extra_usage_enabled": null, "extra_usage_reason": "",
                "live_sessions": 0, "would_use_sessions": 0,
                "windows": windows,
            }));
        }
    }
    (accounts, newest)
}
