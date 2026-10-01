//! The transcript extras: display cells, which reply each artifact card
//! goes under, and whether a pane shows the typing indicator.

use clarp_core::presentation::PresentedRow;
use clarp_engine::Engine;
use serde_json::Value;
use slint::{ModelRc, VecModel};

use crate::{CellLine, DisplayCell};

fn text(value: &Value, key: &str) -> String {
    match value.get(key) {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Number(number)) => number.to_string(),
        _ => String::new(),
    }
}

fn number(value: &Value, key: &str) -> i64 {
    value.get(key).and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))).unwrap_or(0)
}

/// `_explanationRepeat: 0` marks a repeat folded into an earlier row.
pub fn repeated(value: &Value) -> bool {
    value.get("_explanationRepeat").and_then(Value::as_i64) == Some(0)
}

/// DisplayCellCard.qml's fields for one cell.
pub fn display_cell(cell: &Value) -> DisplayCell {
    let lines: Vec<CellLine> = cell
        .get("lines")
        .and_then(Value::as_array)
        .map(|lines| {
            lines
                .iter()
                .map(|line| {
                    let (label, value) = (text(line, "label"), text(line, "text"));
                    CellLine { kind: text(line, "kind").into(), text: if label.is_empty() { value } else { format!("{label}  {value}") }.into() }
                })
                .collect()
        })
        .unwrap_or_default();
    let count = [number(cell, "detail_count"), number(cell, "detailCount")].into_iter().find(|n| *n > 0).unwrap_or(lines.len() as i64);
    let subagent = cell.get("_subagent").filter(|s| s.is_object());
    let title = if text(cell, "title").is_empty() { "Activity".to_owned() } else { text(cell, "title") };
    let status = if text(cell, "status").is_empty() { "recorded".to_owned() } else { text(cell, "status") };
    let (title, summary, phase) = match subagent {
        Some(agent) => {
            let name = text(agent, "name");
            let task = text(agent, "task");
            (if name.is_empty() { title.clone() } else { name }, if task.is_empty() { title } else { task }, text(agent, "phase"))
        }
        None => (title, text(cell, "summary"), String::new()),
    };
    DisplayCell {
        title: title.into(),
        summary: summary.into(),
        status: status.into(),
        more: (count - lines.len() as i64).max(0) as i32,
        lines: ModelRc::new(VecModel::from(lines)),
        subagent: subagent.is_some(),
        phase: phase.into(),
    }
}

/// The cells a row shows; repeats folded into an earlier row are left out.
pub fn cells(row: &PresentedRow) -> Vec<DisplayCell> {
    row.display_cells.iter().filter(|c| !repeated(c)).map(display_cell).collect()
}

/// Tool cards beside display cells: a group shows all of them, otherwise
/// cells stand in for the tools except edits (MessageDelegate.qml).
pub fn shows_tool(row: &PresentedRow, tool: &Value) -> bool {
    !repeated(tool) && (!row.group_label.is_empty() || row.display_cells.is_empty() || matches!(text(tool, "name").as_str(), "Edit" | "MultiEdit" | "Write"))
}

/// For each presented row, the session's artifacts made while it was being
/// written: an artifact belongs to the earliest reply stamped at or after
/// its creation, or the latest reply when it came after them all. Artifacts
/// without a creation time stay out of the transcript.
pub fn artifacts_by_row(rows: &[PresentedRow], artifacts: &[Value]) -> Vec<Vec<Value>> {
    let mut placed = vec![Vec::new(); rows.len()];
    let stamps: Vec<Option<i64>> = rows
        .iter()
        .map(|row| {
            let reply = row.message.role != "user" && !row.activity;
            reply.then(|| clarp_core::protocol::parse_iso_date(&row.message.timestamp).map(|t| t.timestamp_millis())).flatten()
        })
        .collect();
    // The latest reply (the last one stamped latest).
    let last = stamps.iter().enumerate().filter_map(|(i, s)| s.map(|s| (s, i))).max().map(|(_, i)| i);
    for artifact in artifacts {
        let created = number(artifact, "created_at");
        if created <= 0 {
            continue;
        }
        let after = stamps.iter().enumerate().filter_map(|(i, s)| s.filter(|s| *s >= created).map(|s| (s, i))).min_by_key(|(s, _)| *s);
        let at = after.map(|(_, i)| i).or(last);
        if let Some(index) = at {
            placed[index].push(artifact.clone());
        }
    }
    placed
}

/// A pane shows "Working…" while its agent works, when replies are shown
/// only once complete (ConversationPane.qml `working`).
pub fn working(engine: &Engine, session: &str) -> bool {
    let state = engine.roster().display_state(session).unwrap_or_default();
    engine.show_when_ready() && matches!(state.as_str(), "thinking" | "tool" | "compacting" | "running")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn row(id: &str, role: &str, timestamp: &str) -> PresentedRow {
        let message = clarp_core::protocol::Message { id: id.into(), role: role.into(), timestamp: timestamp.into(), ..Default::default() };
        PresentedRow {
            source_row: 0,
            message,
            body: String::new(),
            activity: false,
            tools: Vec::new(),
            display_cells: Vec::new(),
            activity_count: 0,
            group_ids: Vec::new(),
            group_label: String::new(),
            group_expanded: false,
            activity_inline: false,
            activity_label: String::new(),
            explanation_repeat: 1,
        }
    }

    #[test]
    fn an_artifact_goes_under_the_reply_written_while_it_was_made() {
        let at = |t: &str| clarp_core::protocol::parse_iso_date(t).unwrap().timestamp_millis();
        let rows = [
            row("late", "assistant", "2026-09-29T10:05:00Z"),
            row("ask", "user", "2026-09-29T10:00:30Z"),
            row("reply", "assistant", "2026-09-29T10:01:00Z"),
        ];
        let artifacts = [
            json!({"artifact_id": "during", "created_at": at("2026-09-29T10:00:40Z")}),
            json!({"artifact_id": "after", "created_at": at("2026-09-29T11:00:00Z")}),
            json!({"artifact_id": "undated"}),
        ];
        let placed: Vec<Vec<String>> =
            artifacts_by_row(&rows, &artifacts).iter().map(|a| a.iter().map(|v| text(v, "artifact_id")).collect()).collect();
        assert_eq!(placed, [vec!["after".to_owned()], vec![], vec!["during".to_owned()]]);
    }
}
