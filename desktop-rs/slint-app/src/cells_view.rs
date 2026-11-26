//! The transcript extras: display cells, which reply each artifact card
//! goes under, and whether a pane shows the typing indicator.

use clarp_core::presentation::PresentedRow;
use clarp_engine::Engine;
use serde_json::Value;
use slint::{ModelRc, VecModel};

use crate::{CellLine, DisplayCell, HelperLine, HelpersCard};

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
                    // A refused tool's line reads as its reason, without the tags.
                    let value = clarp_core::live_present::refusal(&value).unwrap_or(value);
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

/// The cells a row shows with its tools; repeats folded into an earlier row
/// are left out, and so are its helpers (their own card, `helpers`).
pub fn cells(row: &PresentedRow) -> Vec<DisplayCell> {
    row.display_cells.iter().filter(|c| !repeated(c) && !is_helper(c)).map(display_cell).collect()
}

/// A sub-agent cell: a helper inside the agent's own process.
pub fn is_helper(cell: &Value) -> bool {
    cell.get("kind").and_then(Value::as_str) == Some("subagents")
}

/// The row's helpers as one card (none: `total` 0).
pub fn helpers(row: &PresentedRow) -> HelpersCard {
    let started = clarp_core::protocol::parse_iso_date(&row.message.timestamp).map_or(0, |t| t.timestamp_millis());
    clarp_core::helpers_card::from_cells(&row.message.id, &row.display_cells, started).map(|c| helpers_card(&c)).unwrap_or_default()
}

/// A helpers card as the transcript draws it.
pub fn helpers_card(card: &clarp_core::helpers_card::Card) -> HelpersCard {
    let helpers: Vec<HelperLine> = card
        .helpers
        .iter()
        .map(|h| HelperLine {
            key: h.key.clone().into(),
            name: h.name.clone().into(),
            line: h.line.clone().into(),
            state: h.state.word().into(),
            chat: h.chat.clone().into(),
            lines: ModelRc::new(VecModel::from(
                h.lines.iter().map(|(kind, text)| CellLine { kind: kind.clone().into(), text: text.clone().into() }).collect::<Vec<_>>(),
            )),
        })
        .collect();
    HelpersCard {
        key: card.key.clone().into(),
        running: card.running() as i32,
        total: card.helpers.len() as i32,
        failed: card.failed() as i32,
        timed: card.started_ms > 0,
        started: crate::artifacts_view::clock_at(card.started_ms),
        helpers: ModelRc::new(VecModel::from(helpers)),
    }
}

/// Tool cards beside display cells: a group shows all of them, otherwise
/// cells stand in for the tools except edits (MessageDelegate.qml).
pub fn shows_tool(row: &PresentedRow, tool: &Value) -> bool {
    !repeated(tool) && (!row.group_label.is_empty() || row.display_cells.is_empty() || matches!(text(tool, "name").as_str(), "Edit" | "MultiEdit" | "Write"))
}

/// For each presented row, the session's artifacts made while it was being
/// written: an artifact belongs to the earliest reply stamped at or after
/// its creation, or the latest reply when it came after them all. Artifacts
/// without a creation time stay out of the transcript. A row's cards are in
/// the order they were made, not the list's (newest updated first, so a
/// card that changed would otherwise jump ahead of its neighbours).
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
        // The turn that published it, when the Host names it (Host contract
        // 106): under that turn's last reply, as iOS places it.
        let trace = text(artifact, "source_trace_id");
        let traced = (!trace.trim().is_empty())
            .then(|| rows.iter().enumerate().rev().find(|(i, row)| stamps[*i].is_some() && row.message.trace_id == trace.trim()).map(|(i, _)| i))
            .flatten();
        if let Some(index) = traced {
            placed[index].push(artifact.clone());
            continue;
        }
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
    for row in &mut placed {
        row.sort_by_key(|artifact| number(artifact, "created_at"));
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
    fn an_artifact_naming_its_turn_sits_under_that_turns_reply() {
        let mut rows = vec![
            row("q1", "user", "2026-09-29T10:00:00Z"),
            row("a1", "assistant", "2026-09-29T10:00:30Z"),
            row("q2", "user", "2026-09-29T10:01:00Z"),
            row("a2", "assistant", "2026-09-29T10:01:30Z"),
        ];
        rows[1].message.trace_id = "turn-1".into();
        rows[3].message.trace_id = "turn-2".into();
        // Made late (after both replies), but by the first turn.
        let late = 1_790_676_300_000_i64; // 2026-09-29T10:05:00Z
        let traced = json!({"artifact_id": "f", "created_at": late, "source_trace_id": "turn-1"});
        let untraced = json!({"artifact_id": "g", "created_at": late});
        let placed = artifacts_by_row(&rows, &[traced, untraced]);
        assert_eq!(placed[1].len(), 1);
        assert_eq!(placed[1][0]["artifact_id"], "f");
        assert_eq!(placed[3][0]["artifact_id"], "g");
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

    #[test]
    fn a_rows_cards_keep_the_order_they_were_made_whatever_the_list_order() {
        let rows = [row("reply", "assistant", "2026-09-29T10:01:00Z")];
        // As the Host lists them: the one updated last first.
        let artifacts = [
            json!({"artifact_id": "second", "created_at": 2, "updated_at": 9}),
            json!({"artifact_id": "first", "created_at": 1, "updated_at": 5}),
            json!({"artifact_id": "third", "created_at": 3, "updated_at": 3}),
        ];
        let placed: Vec<String> = artifacts_by_row(&rows, &artifacts)[0].iter().map(|v| text(v, "artifact_id")).collect();
        assert_eq!(placed, ["first", "second", "third"]);
    }
}
