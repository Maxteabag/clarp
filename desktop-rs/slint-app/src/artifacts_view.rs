//! Artifact cards in the chat (iOS `ArtifactCard`): each type's inline
//! fields, the clock countdowns tick on, and what the cards' actions do.

use std::sync::OnceLock;
use std::time::Duration;

use serde_json::Value;
use slint::ComponentHandle;

use clarp_engine::{Change, Engine};
use slint::Model;

use crate::{App, AppWindow, ArtifactBridge, ArtifactItem};

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

/// The epoch second `ArtifactBridge.now` counts from: the cards' clock is
/// small integers, which Slint holds exactly.
fn epoch() -> i64 {
    static START: OnceLock<i64> = OnceLock::new();
    *START.get_or_init(|| chrono::Utc::now().timestamp())
}

/// Seconds on the cards' clock now.
pub fn clock_now() -> i32 {
    (chrono::Utc::now().timestamp() - epoch()) as i32
}

/// iOS `CountdownDisplay`: before the target the time remaining, for a
/// minute after it "Now", then the time since.
pub fn countdown(target: i64, now: i64) -> (&'static str, String) {
    let delta = target - now;
    let clock = |seconds: i64| {
        let (days, rest) = (seconds / 86_400, seconds % 86_400);
        let hms = format!("{:02}:{:02}:{:02}", rest / 3600, rest % 3600 / 60, rest % 60);
        if days > 0 { format!("{days}d {hms}") } else { hms }
    };
    if delta > 0 {
        ("Remaining", clock(delta))
    } else if delta > -60 {
        ("Target reached", "Now".to_owned())
    } else {
        ("Since target", clock(-delta))
    }
}

/// The status worth a badge (iOS shows draft, failed, cancelled, expired).
fn badge(status: &str) -> String {
    match status {
        "draft" | "failed" | "cancelled" | "expired" => {
            let mut chars = status.chars();
            chars.next().map(|c| c.to_uppercase().collect::<String>() + chars.as_str()).unwrap_or_default()
        }
        _ => String::new(),
    }
}

/// The card's fields for one artifact (the Host's flat-v1 shape).
pub fn artifact_item(artifact: &Value) -> ArtifactItem {
    let kind = if text(artifact, "type").is_empty() { "item".to_owned() } else { text(artifact, "type") };
    let status = text(artifact, "status");
    let outcome = [text(artifact, "conclusion"), status.clone()].into_iter().find(|s| !s.is_empty()).unwrap_or_else(|| "unknown".into());
    let failed = matches!(outcome.as_str(), "failed" | "failure" | "timed_out" | "action_required");
    let plan = artifact.get("plan").cloned().unwrap_or(Value::Null);
    let (total, completed) = if kind == "plan" {
        (number(&plan, "total_count"), number(&plan, "completed_count"))
    } else {
        (number(artifact, "total_steps"), number(artifact, "completed_steps"))
    };
    let progress = if !matches!(kind.as_str(), "plan" | "workflow_run") {
        String::new()
    } else if total > 0 {
        format!("{} / {} completed", completed.max(0), total)
    } else if outcome == "active" {
        "In progress".into()
    } else {
        String::new()
    };
    let title = [text(artifact, "file_name"), text(artifact, "title")].into_iter().find(|s| !s.is_empty()).unwrap_or_else(|| "Artifact".into());
    let mut item = ArtifactItem {
        id: text(artifact, "artifact_id").into(),
        label: kind.replace('_', " ").to_uppercase().into(),
        kind: kind.clone().into(),
        outcome: outcome.into(),
        failed,
        badge: badge(&status).into(),
        title: title.into(),
        summary: text(artifact, "summary").into(),
        progress: progress.into(),
        form: kind == "html_form",
        ..ArtifactItem::default()
    };
    match kind.as_str() {
        "countdown" => countdown_fields(&mut item, artifact, &status),
        "html_form" => {
            let report = is_report(artifact);
            item.label = if report { "REPORT" } else { "FORM" }.into();
            item.action = if report { "Open report" } else { "Open form" }.into();
            item.form = false;
        }
        _ => {}
    }
    item
}

/// A card as the chat shows it: its fields, whether the keyboard is on it,
/// and how its latest action went.
pub fn card(artifact: &Value, engine: &Engine, cursor: &str) -> ArtifactItem {
    let mut item = artifact_item(artifact);
    item.selected = !cursor.is_empty() && item.id == cursor;
    item.status_text = engine.form_status(&item.id).into();
    item
}

/// What a card's row depends on beyond the artifact itself.
pub fn card_signature(artifact: &Value, engine: &Engine, cursor: &str) -> String {
    let id = text(artifact, "artifact_id");
    format!("{}{}", if id == cursor { "*" } else { "" }, engine.form_status(&id))
}

/// iOS `isHTMLReport`: read-only, or an answer schema that asks nothing.
fn is_report(artifact: &Value) -> bool {
    let flagged = |v: &Value| v.get("read_only").and_then(Value::as_bool) == Some(true);
    if flagged(artifact) || artifact.get("payload").is_some_and(flagged) {
        return true;
    }
    let schema = artifact.get("answer_schema").cloned().unwrap_or(Value::Null);
    let fields = schema.get("properties").and_then(Value::as_object).map(|p| p.len());
    fields == Some(0) || (fields.is_none() && schema.get("additionalProperties") == Some(&Value::Bool(false)))
}

/// The open chat's card ids, top to bottom.
fn card_ids(app: &App) -> Vec<String> {
    app.active_messages()
        .map(|rows| rows.iter().flat_map(|row| row.artifacts.iter().map(|a| a.id.to_string()).collect::<Vec<_>>()).collect())
        .unwrap_or_default()
}

pub fn has_cards(app: &App) -> bool {
    !card_ids(app).is_empty()
}

/// The card the keyboard is on, while it is in the open chat.
pub fn selected(app: &App) -> Option<String> {
    let cursor = app.artifact_cursor.borrow().clone();
    (!cursor.is_empty() && card_ids(app).contains(&cursor)).then_some(cursor)
}

/// J/K: the next or previous card; from none, the latest.
pub fn step(app: &App, direction: i32) {
    let ids = card_ids(app);
    let Some(last) = ids.len().checked_sub(1) else { return };
    let at = selected(app).and_then(|id| ids.iter().position(|i| *i == id));
    let next = match at {
        None => last,
        Some(index) => (index as i64 + i64::from(direction)).clamp(0, last as i64) as usize,
    };
    *app.artifact_cursor.borrow_mut() = ids[next].clone();
    app.refresh(&[Change::Updates]);
}

/// A card's action (Enter on the selected card, or a click).
pub fn open(app: &App, window: &AppWindow, id: &str) {
    let artifact = app.engine.borrow().update_artifacts().iter().find(|a| text(a, "artifact_id") == id).cloned();
    let Some(artifact) = artifact else {
        eprintln!("clarp-slint: no artifact {id} to open");
        return;
    };
    *app.artifact_cursor.borrow_mut() = id.to_owned();
    match text(&artifact, "type").as_str() {
        "html_form" if !is_report(&artifact) => {
            let version = artifact.get("version").cloned().unwrap_or(Value::Null);
            match crate::form_server::serve(id, version, &text(&artifact, "content")) {
                Ok(url) => crate::open_link(&url),
                Err(error) => {
                    eprintln!("clarp-slint: {error}");
                    app.engine.borrow_mut().set_form_status(id, &format!("Not opened: {error}"));
                }
            }
        }
        _ => crate::updates_view::open_report(app, window, id),
    }
    app.refresh(&[Change::Updates]);
}

/// A countdown's target on the cards' clock, and its date in the target's
/// own offset (the Host requires one) with the zone it names.
fn countdown_fields(item: &mut ArtifactItem, artifact: &Value, status: &str) {
    if status == "cancelled" {
        item.countdown_note = "Cancelled".into();
        return;
    }
    let Ok(target) = chrono::DateTime::parse_from_rfc3339(&text(artifact, "target_at")) else {
        item.countdown_note = "Countdown unavailable".into();
        return;
    };
    let zone = text(artifact, "time_zone");
    item.countdown_set = true;
    item.countdown_at = (target.timestamp() - epoch()).clamp(i64::from(i32::MIN), i64::from(i32::MAX)) as i32;
    item.countdown_line = format!("{} · {}", target.format("%b %-d, %Y at %H:%M"), if zone.is_empty() { "UTC" } else { &zone }).into();
}

/// The bridge's clock and callbacks.
pub fn bind(window: &AppWindow) {
    let bridge = window.global::<ArtifactBridge>();
    bridge.on_countdown_phase(|target, now| countdown(i64::from(target), i64::from(now)).0.into());
    bridge.on_countdown_clock(|target, now| countdown(i64::from(target), i64::from(now)).1.into());
    bridge.set_now(clock_now());
    bridge.on_open(|id| {
        if let (Some(app), Some(window)) = (crate::app(), crate::window()) {
            open(&app, &window, &id);
        }
    });
    let weak = window.as_weak();
    let timer = slint::Timer::default();
    // A quarter-second check keeps the tick within 250 ms of the second.
    timer.start(slint::TimerMode::Repeated, Duration::from_millis(250), move || {
        if let Some(window) = weak.upgrade() {
            let bridge = window.global::<ArtifactBridge>();
            let now = clock_now();
            if bridge.get_now() != now {
                bridge.set_now(now);
            }
        }
    });
    CLOCK.with(|clock| *clock.borrow_mut() = Some(timer));
}

thread_local! {
    static CLOCK: std::cell::RefCell<Option<slint::Timer>> = const { std::cell::RefCell::new(None) };
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_countdown_counts_down_then_up() {
        assert_eq!(countdown(100 + 86_400 + 3 * 3600 + 4 * 60 + 5, 100), ("Remaining", "1d 03:04:05".to_owned()));
        assert_eq!(countdown(100, 99), ("Remaining", "00:00:01".to_owned()));
        assert_eq!(countdown(100, 100), ("Target reached", "Now".to_owned()));
        assert_eq!(countdown(100, 159), ("Target reached", "Now".to_owned()));
        assert_eq!(countdown(100, 160), ("Since target", "00:01:00".to_owned()));
    }

    #[test]
    fn only_unfinished_states_get_a_badge() {
        assert_eq!(badge("cancelled"), "Cancelled");
        assert_eq!(badge("active"), "");
    }
}
