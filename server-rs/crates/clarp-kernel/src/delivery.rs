//! What a client is shown of a user send's delivery (the feature `send_delivery_state`,
//! contract 81): whether the send waits to be handed to its tool, was handed over, or was
//! stopped by the user before it was, read as the Python Host reads it (`launch_receipts`,
//! `Handler._add_delivery_state`, `sse_events._egress`) and held to its answers by vectors.
//!
//! The Python Host keeps a receipt for each user send. This module takes a receipt as that
//! Host would store it (a JSON object: `state`, `agent_id`, `trace_id`, `client_msg_id`,
//! `origin`, `sender_agent_id`, `user_stop_gen`, `rev`, `seq`), whatever made it: the Rust
//! Host makes one from the agent's own log. A feed of them is read through [`Feed`], which a
//! Host answers from what it holds, a page at a time.

use serde_json::{Map, Value, json};

use crate::python;

/// Saved, waiting to be handed to its tool: a display hint, never permission to send again.
pub const PENDING_LAUNCH: &str = "pending_launch";
/// Withdrawn by the user's Stop before it was handed over.
pub const STOPPED: &str = "stopped";
/// Handed to its tool; how the turn went is not said here.
pub const HANDED_OVER: &str = "handed_over";
/// The most a page of the feed holds.
pub const PAGE_CAP: usize = 200;
/// The release a Host that owns its turns itself names in its scope.
pub const IN_PROCESS_RELEASE: &str = "in-process";
/// The event that tells a client a send's delivery may have changed.
pub const HINT: &str = "delivery-updated";

/// A stored rev, seq or generation: a positive whole number, else 0.
fn count(value: &Value) -> u64 {
    value.as_u64().filter(|n| *n > 0).unwrap_or(0)
}

/// A whole number as Python's `json` reads one (`isinstance(value, int)`, not a bool).
fn whole(value: &Value) -> Option<i128> {
    match value {
        Value::Number(number) if !number.is_f64() => number
            .as_i64()
            .map(i128::from)
            .or_else(|| number.as_u64().map(i128::from)),
        _ => None,
    }
}

/// The id of the row a client's send is stored under: its own id, with `u-` before it.
pub fn message_id(client_msg_id: &str) -> String {
    let id = python::strip(client_msg_id);
    if id.starts_with("u-") {
        id.to_string()
    } else {
        format!("u-{id}")
    }
}

/// A stored receipt's text as a receipt: a JSON object, else nothing.
pub fn parse(raw: Option<&str>) -> Option<Value> {
    let raw = raw.filter(|raw| !raw.is_empty())?;
    serde_json::from_str::<Value>(raw)
        .ok()
        .filter(Value::is_object)
}

/// What a client is shown of a receipt, at the agent's user Stop generation: the delivery
/// object, or nothing (unknown). `origin` and `sender` are a row's, where a row asks.
pub fn project(
    receipt: &Value,
    agent_id: &str,
    generation: i64,
    origin: Option<&str>,
    sender: Option<&str>,
) -> Option<Value> {
    let receipt = receipt.as_object()?;
    let field = |key: &str| receipt.get(key).unwrap_or(&Value::Null);
    let (rev, seq) = (count(field("rev")), count(field("seq")));
    let client_msg_id = field("client_msg_id")
        .as_str()
        .filter(|id| !id.is_empty())?;
    let trace_id = field("trace_id").as_str().filter(|id| !id.is_empty())?;
    if rev == 0 || seq == 0 || field("agent_id").as_str() != Some(agent_id) {
        return None;
    }
    if origin.is_some_and(|origin| field("origin").as_str() != Some(origin)) {
        return None;
    }
    if let Some(sender) = sender {
        let theirs = field("sender_agent_id");
        let said = if python::truthy(theirs) {
            theirs.clone()
        } else {
            json!("")
        };
        if said != json!(sender) {
            return None;
        }
    }
    let shown = match field("state").as_str() {
        Some("attempted") => HANDED_OVER,
        Some("fenced") => STOPPED,
        Some("pending") => {
            let admitted = whole(field("user_stop_gen"))?;
            if i128::from(generation) > admitted {
                STOPPED
            } else {
                PENDING_LAUNCH
            }
        }
        _ => return None,
    };
    Some(json!({
        "state": shown,
        "rev": rev,
        "seq": seq,
        "for": {"client_msg_id": client_msg_id, "trace_id": trace_id},
    }))
}

/// Gives the user rows of one `GET /log` page their delivery: a row whose trace has a receipt
/// that is shown, of this very row (its trace, and its id the receipt's message id). `receipt`
/// answers for a trace. Answers the generation the rows were shown at: the page's epoch.
pub fn annotate(
    agent_id: &str,
    turns: &mut [Value],
    generation: i64,
    receipt: impl Fn(&str) -> Option<Value>,
) -> i64 {
    for turn in turns.iter_mut() {
        let text = |key: &str| turn[key].as_str().unwrap_or("").to_string();
        let (trace, id) = (text("trace_id"), text("id"));
        if turn["role"] != "user" || trace.is_empty() || id.is_empty() {
            continue;
        }
        let origin = if python::truthy(&turn["origin"]) {
            text("origin")
        } else {
            "user".to_string()
        };
        let sender = if python::truthy(&turn["sender_agent_id"]) {
            text("sender_agent_id")
        } else {
            String::new()
        };
        let Some(stored) = receipt(&trace) else {
            continue;
        };
        let Some(shown) = project(&stored, agent_id, generation, Some(&origin), Some(&sender))
        else {
            continue;
        };
        let of_it = shown["for"]["trace_id"] == trace.as_str()
            && message_id(shown["for"]["client_msg_id"].as_str().unwrap_or("")) == id;
        if of_it {
            turn["delivery"] = shown;
        }
    }
    generation
}

/// What a page of the feed is read from: a Host answers each from what it holds, and reads
/// no more for a page than the page.
pub trait Feed {
    /// The newest seq there is.
    fn current(&self) -> u64;
    /// Below this, what was let go may be missing: a cursor behind it has expired.
    fn watermark(&self) -> u64;
    /// The agent's user Stop generation.
    fn generation(&self) -> i64;
    /// The agent's pointers after `after` up to and with `through`, in seq order, at most
    /// `cap`: each with the trace it names and that trace's receipt, where there is one.
    fn pointers(&self, after: u64, through: u64, cap: usize) -> Vec<(u64, String, Option<Value>)>;
}

/// One page of the feed, as `delivery_page` answers it.
#[derive(Debug, Clone, PartialEq)]
pub struct Page {
    pub updates: Vec<Value>,
    pub cursor: Option<u64>,
    pub more: bool,
    pub expired: bool,
    pub until: Option<u64>,
    pub stop_generation: Option<i64>,
    pub skipped: u64,
}

impl Page {
    fn expired() -> Self {
        Self {
            updates: Vec::new(),
            cursor: None,
            more: false,
            expired: true,
            until: None,
            stop_generation: None,
            skipped: 0,
        }
    }
}

/// One page of the agent's feed in seq order (`delivery_page`). The cursor is valid only for
/// the user Stop generation it was taken under; a bootstrap pass fixes `until` on its first
/// page. A number too great for a seq is `u64::MAX`, beyond any there is.
pub fn page(
    feed: &dyn Feed,
    agent_id: &str,
    since: i128,
    until: Option<u64>,
    stop_generation: Option<i128>,
    bootstrap: bool,
    cap: usize,
) -> Page {
    let cap = cap.max(1);
    let current = feed.current();
    let watermark = feed.watermark();
    let generation = feed.generation();
    if since < 0 || since > i128::from(current) {
        return Page::expired();
    }
    let since = u64::try_from(since).unwrap_or(u64::MAX);
    let first_bootstrap_page = bootstrap && until.is_none();
    let until = if first_bootstrap_page {
        if since != 0 {
            return Page::expired();
        }
        Some(current)
    } else {
        if stop_generation != Some(i128::from(generation)) {
            return Page::expired();
        }
        until
    };
    let high = if bootstrap {
        let Some(until) = until.filter(|until| since <= *until && *until <= current) else {
            return Page::expired();
        };
        until
    } else {
        if since >= 1 && since < watermark {
            return Page::expired();
        }
        current
    };
    let rows = feed.pointers(since, high, cap);
    let mut updates = Vec::new();
    let mut skipped = 0;
    for (seq, trace, receipt) in &rows {
        let holds = receipt.as_ref().filter(|receipt| {
            count(&receipt["seq"]) == *seq
                && receipt["trace_id"] == trace.as_str()
                && receipt["agent_id"] == agent_id
        });
        let Some(receipt) = holds else {
            skipped += 1;
            continue;
        };
        if let Some(shown) = project(receipt, agent_id, generation, None, None) {
            let id = message_id(shown["for"]["client_msg_id"].as_str().unwrap_or(""));
            updates.push(json!({"message_id": id, "delivery": shown}));
        }
    }
    let (cursor, more) = match rows.last() {
        Some((seq, _, _)) if rows.len() == cap => (*seq, true),
        _ => (high, false),
    };
    Page {
        updates,
        cursor: Some(cursor),
        more,
        expired: false,
        until: if bootstrap { until } else { None },
        stop_generation: Some(generation),
        skipped,
    }
}

/// A `delivery_*` number of a query: none where it is not given, else plain ASCII digits
/// (`u64::MAX` where they are more than a seq can be). Anything else: `bad <name>`, the
/// Python Host's 400.
pub fn number(name: &str, raw: Option<&str>) -> Result<Option<u128>, String> {
    let raw = python::strip(raw.unwrap_or(""));
    if raw.is_empty() {
        return Ok(None);
    }
    if !(raw.is_ascii() && python::isdigit(raw)) {
        return Err(format!("bad {name}"));
    }
    Ok(Some(raw.parse::<u128>().unwrap_or(u128::MAX)))
}

/// What a `GET /log` query asks of the feed, read as the Python Host reads it.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Query {
    pub since: Option<u128>,
    pub until: Option<u128>,
    pub generation: Option<u128>,
    pub bootstrap: bool,
    /// The runtime scope the client's cursor was taken under: release and instance.
    pub carried: (String, String),
}

impl Query {
    /// Reads the parameters; `get` answers a parameter's first value. Err: the 400's words.
    pub fn read<'a>(get: impl Fn(&str) -> Option<&'a str>) -> Result<Self, String> {
        let since = number("delivery_since", get("delivery_since"))?;
        let until = number("delivery_until", get("delivery_until"))?;
        let generation = number("delivery_stop_generation", get("delivery_stop_generation"))?;
        let flag = python::strip(get("delivery_bootstrap").unwrap_or(""));
        let text = |name: &str| python::strip(get(name).unwrap_or("")).to_string();
        Ok(Self {
            since,
            until,
            generation,
            bootstrap: flag == "1" || flag == "true",
            carried: (
                text("delivery_runtime_release"),
                text("delivery_runtime_instance"),
            ),
        })
    }
}

/// Who answers: the scope a Host names, owning its turns itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Scope {
    pub server_id: String,
    pub agent_id: String,
    pub release: String,
    pub instance: String,
}

/// The delivery fields of one `GET /log` answer, in the in-process mode: the user rows of
/// `turns` are given their delivery, and the fields to add to the answer are returned. A page
/// of the feed is read where the query asks for one; a cursor taken under another scope has
/// expired.
pub fn answer(
    feed: &dyn Feed,
    scope: &Scope,
    turns: &mut [Value],
    query: &Query,
    receipt: impl Fn(&str) -> Option<Value>,
) -> Map<String, Value> {
    // The user rows are shown on copies, and what is shown is given to the rows of the answer
    // by their id, as the Python Host merges it.
    let mut users: Vec<Value> = turns
        .iter()
        .filter(|turn| turn["role"] == "user")
        .cloned()
        .collect();
    let epoch = annotate(&scope.agent_id, &mut users, feed.generation(), receipt);
    let mut shown = Map::new();
    for user in &users {
        if let (Some(id), Some(delivery)) = (user["id"].as_str(), user.get("delivery")) {
            shown.insert(id.to_string(), delivery.clone());
        }
    }
    for turn in turns.iter_mut() {
        let id = turn["id"].as_str().unwrap_or("").to_string();
        if turn["role"] == "user"
            && let Some(delivery) = shown.get(&id)
        {
            turn["delivery"] = delivery.clone();
        }
    }
    let mut top = Map::new();
    top.insert(
        "delivery_scope".into(),
        json!({"server_id": scope.server_id, "agent_id": scope.agent_id,
               "runtime_release": scope.release, "runtime_instance": scope.instance}),
    );
    top.insert("delivery_stop_generation".into(), json!(epoch));
    let Some(since) = query.since else {
        return top;
    };
    // A plain since=0 is the first bootstrap page.
    let bootstrap = query.bootstrap || since == 0;
    let wide = |n: u128| u64::try_from(n).unwrap_or(u64::MAX);
    let since_i = i128::try_from(since).unwrap_or(i128::MAX);
    let generation = query
        .generation
        .map(|g| i128::try_from(g).unwrap_or(i128::MAX));
    let mut page = page(
        feed,
        &scope.agent_id,
        since_i,
        query.until.map(wide),
        generation,
        bootstrap,
        PAGE_CAP,
    );
    let continuation = !(bootstrap && since == 0 && query.until.is_none());
    if continuation && query.carried != (scope.release.clone(), scope.instance.clone()) {
        page = Page::expired();
    }
    top.insert(
        "delivery_updates".into(),
        Value::Array(page.updates.clone()),
    );
    top.insert("delivery_cursor".into(), json!(page.cursor));
    top.insert("delivery_more".into(), json!(page.more));
    top.insert("delivery_cursor_expired".into(), json!(page.expired));
    if bootstrap && !page.expired {
        top.insert("delivery_bootstrap".into(), json!(true));
        top.insert("delivery_until".into(), json!(page.until));
    }
    top
}

/// A stored event on its way out (`sse_events._egress`): a `delivery-updated` hint leaves with
/// its identity alone, and not at all where its identity is missing or not a non-empty text;
/// any other event leaves as it was stored. `internal`: the Host's own relay, which also keeps
/// the runtime's mark.
pub fn egress(stored: &Value, internal: bool) -> Option<Value> {
    if stored["type"] != HINT {
        return Some(stored.clone());
    }
    let identity = ["session", "agent_id", "message_id"];
    let named = identity
        .iter()
        .all(|key| stored[*key].as_str().is_some_and(|text| !text.is_empty()));
    if !named {
        return None;
    }
    let mut hint = Map::new();
    hint.insert("type".into(), json!(HINT));
    for key in identity {
        hint.insert(key.into(), stored[key].clone());
    }
    hint.insert("ts".into(), stored["ts"].clone());
    hint.insert("event_id".into(), stored["event_id"].clone());
    let mark = "_clarp_runtime_event";
    if internal && python::truthy(&stored[mark]) {
        hint.insert(mark.into(), stored[mark].clone());
    }
    Some(Value::Object(hint))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_clients_id_is_its_rows_id_with_one_prefix() {
        assert_eq!(message_id("m1"), "u-m1");
        assert_eq!(message_id(" u-m1 "), "u-m1");
        assert_eq!(message_id(""), "u-");
    }
}
