//! Why a turn ended (Rust Host contract 162, feature `turn_end_reason`,
//! issues 729 and 731): the row that closes a turn that did not complete
//! carries `turn.reason`, the Host's own sentence ("Stopped by its user.",
//! "Stopped for a newer message.", a failure, a restart or move). The chat
//! shows it as one quiet line under the turn, also when the turn did no work
//! at all (it was stopped while it waited for an account). A completed turn
//! never shows a line, and without the feature nothing here runs. Pure: the
//! window inserts the lines among the rows it shows.

use serde_json::Value;

use crate::protocol::Message;

pub const FEATURE: &str = "turn_end_reason";

/// One line to show: the key it is drawn and kept by, and the Host's words.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Line {
    /// `turn-reason:<turn id>`.
    pub key: String,
    pub turn_id: String,
    pub text: String,
}

/// What the window shows of one row: enough to place the lines.
#[derive(Debug, Clone, Copy)]
pub struct Shown<'a> {
    pub role: &'a str,
    pub trace_id: &'a str,
    /// The text the row shows (empty for a work row).
    pub text: &'a str,
    /// Not sent yet (or its send failed): the lines go above it.
    pub unsent: bool,
}

impl<'a> Shown<'a> {
    pub fn of(message: &'a Message, text: &'a str) -> Self {
        Self { role: &message.role, trace_id: &message.trace_id, text, unsent: message.pending || message.delivery_failed }
    }
}

pub fn key(turn_id: &str) -> String {
    format!("turn-reason:{turn_id}")
}

fn text<'a>(turn: &'a crate::json::Object, key: &str) -> &'a str {
    turn.get(key).and_then(Value::as_str).unwrap_or_default()
}

/// The ended turns that say why, in the order they ended: the last summary
/// a turn's rows carry is its end. A completed turn, or an end without a
/// reason (written before contract 162), gives none.
pub fn ended(rows: &[Message]) -> Vec<Line> {
    let mut lines: Vec<Line> = Vec::new();
    for turn in rows.iter().filter_map(|row| row.turn.as_ref()) {
        let turn_id = text(turn, "turn_id");
        if turn_id.is_empty() {
            continue;
        }
        lines.retain(|line| line.turn_id != turn_id);
        let reason = text(turn, "reason").trim();
        if text(turn, "status") == "completed" || reason.is_empty() {
            continue;
        }
        lines.push(Line { key: key(turn_id), turn_id: turn_id.to_owned(), text: reason.to_owned() });
    }
    lines
}

/// Where each line goes among the shown rows (an index to insert at, in
/// the rows as they are): at the end of its turn, before the first row
/// after it that is not its own: the next prompt, an unsent message, or a
/// row of another turn (the answer and Working row of a turn that took over
/// with no prompt row between). Its own end written later (its closing
/// summary, after the new turn began) does not carry the line down there;
/// with nothing after it, under its last own row. A turn none of
/// whose rows is shown (its prompt on an older page) gets no line, nor one
/// whose own shown row says the same words already (a failure's row).
pub fn place(lines: &[Line], shown: &[Shown<'_>]) -> Vec<(usize, Line)> {
    let mut placed = Vec::new();
    for line in lines {
        let own = |row: &Shown<'_>| row.trace_id == line.turn_id;
        let said = shown.iter().any(|row| own(row) && row.role != "user" && row.text.contains(line.text.as_str()));
        if said {
            continue;
        }
        let (Some(first), Some(last)) = (shown.iter().position(own), shown.iter().rposition(own)) else { continue };
        let other = |row: &Shown<'_>| row.unsent || (row.role == "user" && !own(row)) || (!row.trace_id.is_empty() && !own(row));
        let at = shown.iter().enumerate().skip(first + 1).find(|(_, row)| other(row)).map_or(last + 1, |(i, _)| i);
        placed.push((at, line.clone()));
    }
    placed.sort_by_key(|(at, _)| *at);
    placed
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn row(id: &str, role: &str, trace: &str, text: &str, turn: Option<Value>) -> Message {
        Message {
            id: id.into(),
            role: role.into(),
            trace_id: trace.into(),
            text: text.into(),
            display_text: text.into(),
            turn: turn.and_then(|t| t.as_object().cloned()),
            ..Message::default()
        }
    }

    fn shown(rows: &[Message]) -> Vec<Shown<'_>> {
        rows.iter().map(|r| Shown::of(r, &r.display_text)).collect()
    }

    #[test]
    fn a_turn_stopped_before_any_work_says_why_under_its_prompt() {
        // A parked turn (waiting for an account): its prompt closes it.
        let stopped = json!({"turn_id": "t1", "status": "interrupted", "tool_count": 0, "reason": "Stopped by its user."});
        let rows = vec![row("u1", "user", "t1", "hello", Some(stopped)), row("u2", "user", "t2", "again", None)];
        let lines = ended(&rows);
        assert_eq!(lines, vec![Line { key: "turn-reason:t1".into(), turn_id: "t1".into(), text: "Stopped by its user.".into() }]);
        let placed = place(&lines, &shown(&rows));
        assert_eq!(placed.len(), 1);
        assert_eq!(placed[0].0, 1, "between the stopped prompt and the next one");
    }

    #[test]
    fn a_completed_turn_and_an_old_end_show_nothing() {
        let done = json!({"turn_id": "t1", "status": "completed", "reason": "ignored"});
        let old = json!({"turn_id": "t2", "status": "failed"});
        let rows = vec![
            row("u1", "user", "t1", "hi", None),
            row("a1", "assistant", "t1", "done", Some(done)),
            row("u2", "user", "t2", "hi", None),
            row("a2", "assistant", "t2", "", Some(old)),
        ];
        assert!(ended(&rows).is_empty());
    }

    #[test]
    fn the_line_goes_after_the_turns_rows_and_before_unsent_messages() {
        let stopped = json!({"turn_id": "t1", "status": "interrupted", "reason": "Stopped for a newer message."});
        let mut unsent = row("p", "user", "", "draft", None);
        unsent.pending = true;
        let rows = vec![row("u1", "user", "t1", "go", None), row("a1", "assistant", "t1", "working on it", Some(stopped)), unsent];
        let placed = place(&ended(&rows), &shown(&rows));
        assert_eq!(placed[0].0, 2);
    }

    #[test]
    fn a_turn_taken_over_keeps_its_line_above_the_new_answer() {
        // Send now stopped t1 for a waiting message: t2's answer and its
        // work follow with no prompt row between (an answer with no trace
        // too); the line stays with t1.
        let stopped = json!({"turn_id": "t1", "status": "interrupted", "reason": "Stopped for a newer message."});
        let rows = vec![
            row("u1", "user", "t1", "go", None),
            row("a1", "assistant", "t1", "half an answer", Some(stopped)),
            row("w2", "assistant", "t2", "", None),
            row("a2", "assistant", "", "the new answer", None),
            row("e1", "assistant", "t1", "", None),
        ];
        let placed = place(&ended(&rows), &shown(&rows));
        assert_eq!(placed[0].0, 2, "right under the stopped turn: {placed:?}");
    }

    #[test]
    fn a_failure_its_row_already_says_is_not_said_twice() {
        let failed = json!({"turn_id": "t1", "status": "failed", "reason": "The model is overloaded."});
        let rows = vec![row("u1", "user", "t1", "go", None), row("n1", "assistant", "t1", "The model is overloaded.", Some(failed))];
        assert!(place(&ended(&rows), &shown(&rows)).is_empty());
    }

    #[test]
    fn a_turn_with_no_row_shown_gets_no_line() {
        let stopped = json!({"turn_id": "t1", "status": "interrupted", "reason": "Stopped by its user."});
        let rows = vec![row("a1", "assistant", "t1", "", Some(stopped))];
        let lines = ended(&rows);
        assert!(place(&lines, &[]).is_empty());
    }

    #[test]
    fn a_later_end_of_the_same_turn_wins() {
        let stopped = json!({"turn_id": "t1", "status": "interrupted", "reason": "Stopped by its user."});
        let done = json!({"turn_id": "t1", "status": "completed"});
        let rows = vec![row("u1", "user", "t1", "go", Some(stopped)), row("a1", "assistant", "t1", "ok", Some(done))];
        assert!(ended(&rows).is_empty());
    }
}
