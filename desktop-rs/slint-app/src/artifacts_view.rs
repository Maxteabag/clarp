//! Artifact cards in the chat (iOS `ArtifactCard`): each type's inline
//! fields, the clock countdowns tick on, and what the cards' actions do.

use std::sync::OnceLock;
use std::time::Duration;

use serde_json::Value;
use slint::ComponentHandle;

use crate::{AppWindow, ArtifactBridge, ArtifactItem};

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
    if kind == "countdown" {
        countdown_fields(&mut item, artifact, &status);
    }
    item
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
