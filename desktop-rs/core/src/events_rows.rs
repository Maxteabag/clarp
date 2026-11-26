//! Rows carried in `transcript-updated` (feature `events_rows`, Rust Host
//! contract 199; docs/protocol.md "Rows carried in `transcript-updated`").
//!
//! A stream opened with `rows=1` gets, on a `transcript-updated`, the rows
//! `GET /log?after_revision=<rows_from>&include_automated=1&include_tool_details=0`
//! answers. The desktop reads `/log` with `include_automated=0` and every
//! tool's details, so it takes the carried rows only where they are exactly
//! what its own read would give:
//!
//! - it holds the event's conversation at `rows_from` or later (no gap);
//! - no row it would draw has tools or cells: those come shortened in the
//!   event (headers only), and the desktop draws them whole;
//! - an automated row is dropped by the desktop whichever way it comes (the
//!   Host leaves most of them out of `include_automated=0`, marks every one
//!   `automated`, and the desktop never draws a marked row), so those may
//!   have anything.
//!
//! Anything else (no rows: a withheld agent, a replay after a reconnect, a
//! large change, an older Host) reads `/log` as before.

use serde_json::{Value, json};

use crate::json::{self, Object};

/// The `/server-info` feature that makes `rows=1` mean something.
pub const FEATURE: &str = "events_rows";

/// The `/log` delta answer the event's rows stand for, when the desktop may
/// apply it in place of reading `/log`: the conversation it holds is
/// `conversation_id` at `held_revision`.
pub fn as_delta(event: &Object, conversation_id: &str, held_revision: i64) -> Option<Object> {
    let rows = event.get("rows")?.as_array()?;
    let latest = event.get("rows_latest").and_then(Value::as_i64)?;
    let from = event.get("rows_from").and_then(Value::as_i64).filter(|from| *from > 0)?;
    let carried = json::string(event, "conversation_id");
    if conversation_id.is_empty() || carried != conversation_id || held_revision < from {
        return None;
    }
    if !rows.iter().all(drawn_alike) {
        return None;
    }
    json!({
        "conversation_id": carried,
        "turns": rows,
        "latest_revision": latest,
        "has_more": false,
        "replace_required": false,
    })
    .as_object()
    .cloned()
}

/// Whether a carried row draws as the desktop's own `/log` read would draw
/// it: an automated one is never drawn, any other has no tools to shorten.
fn drawn_alike(row: &Value) -> bool {
    let Some(row) = row.as_object() else { return false };
    if crate::protocol::automated_row(row) {
        return true;
    }
    let empty = |key: &str| row.get(key).is_none_or(|value| value.is_null() || value.as_array().is_some_and(Vec::is_empty));
    empty("tools") && empty("display_cells") && !json::boolean(row, "tool_details_available")
}
