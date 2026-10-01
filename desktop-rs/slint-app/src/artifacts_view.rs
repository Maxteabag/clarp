//! Artifact cards in the chat (iOS `ArtifactCard`): each type's inline
//! fields, the clock countdowns tick on, and what the cards' actions do.

use std::sync::OnceLock;
use std::time::Duration;

use serde_json::Value;
use slint::ComponentHandle;

use clarp_engine::{Change, Engine};
use slint::Model;

use crate::{App, AppWindow, ArtifactBridge, ArtifactItem, ArtifactOption};

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
        "countdown" => {
            countdown_fields(&mut item, artifact, &status);
            set_note(&mut item, &text(artifact, "content"));
        }
        "decision" | "question" => decision_fields(&mut item, artifact),
        "plan" => plan_fields(&mut item, artifact),
        "document" | "research" => {
            let content = text(artifact, "content");
            if !content.trim().is_empty() {
                // HTML reads as its text, Markdown without its marks.
                let markdown = if clarp_core::text::looks_like_html_report(&content) { crate::updates_view::html_markdown(&content) } else { content };
                item.preview = clarp_core::text::plain_preview_text(&markdown).chars().take(600).collect::<String>().into();
                item.action = if kind == "document" { "Open document" } else { "Open research" }.into();
            }
            let sources = https_sources(artifact).len();
            if sources > 0 {
                item.sources = format!("{sources} source{}", if sources == 1 { "" } else { "s" }).into();
            }
        }
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

/// The Markdown body the card shows under its summary.
fn set_note(item: &mut ArtifactItem, markdown: &str) {
    let markdown = markdown.trim();
    item.note = markdown.into();
    item.note_styled = crate::view::styled(markdown, false);
}

/// What the window holds for the cards beyond the artifacts: the card the
/// keyboard is on and the answers chosen on cards.
pub struct CardState<'a> {
    pub cursor: &'a str,
    pub choices: &'a std::collections::HashMap<String, i32>,
    /// Answers of one's own as typed, and the card whose field is open.
    pub drafts: &'a std::collections::HashMap<String, String>,
    pub editing: &'a str,
    /// Decisions this window has shown pending.
    pub seen_pending: &'a std::collections::HashSet<String>,
}

/// A card as the chat shows it: its fields, whether the keyboard is on it,
/// what it chose and how its latest action went.
pub fn card(artifact: &Value, engine: &Engine, state: &CardState) -> ArtifactItem {
    let mut item = artifact_item(artifact);
    item.selected = !state.cursor.is_empty() && item.id == state.cursor;
    item.status_text = engine.artifact_status(&item.id).into();
    if item.pending {
        let choices = item.options.row_count() + usize::from(item.allow_custom);
        item.chosen = state.choices.get(item.id.as_str()).copied().filter(|c| (*c as usize) < choices).unwrap_or(-1);
        item.draft = state.drafts.get(item.id.as_str()).cloned().unwrap_or_default().into();
        item.editing = item.allow_custom && state.editing == item.id.as_str();
    }
    // Answered in this window, it keeps the line it had, so its height.
    item.keep_line = (item.pending && !item.meta.is_empty()) || !item.delivery.is_empty() || state.seen_pending.contains(item.id.as_str());
    item
}

/// Brings a reply's card model to `cards`, in place when the same cards
/// are there (only the ones that changed are set).
pub fn update_cards(model: &slint::VecModel<ArtifactItem>, cards: Vec<ArtifactItem>) {
    let same = model.row_count() == cards.len() && cards.iter().enumerate().all(|(i, c)| model.row_data(i).is_some_and(|m| m.id == c.id));
    if !same {
        model.set_vec(cards);
        return;
    }
    for (index, card) in cards.into_iter().enumerate() {
        if model.row_data(index).as_ref() != Some(&card) {
            model.set_row_data(index, card);
        }
    }
}

/// iOS `DecisionResponseView`: while pending the question, why and how
/// urgent, and its answers; once answered, how and with what.
fn decision_fields(item: &mut ArtifactItem, artifact: &Value) {
    let Some(decision) = artifact.get("decision").filter(|d| d.is_object()) else { return };
    let status = text(decision, "status");
    item.question = text(decision, "question").into();
    item.context = text(decision, "context").into();
    let response = text(decision, "response_type");
    item.approval = response.is_empty() || response == "approval";
    let options: Vec<ArtifactOption> = if item.approval {
        let label = |key: &str, fallback: &str| Some(text(decision, key)).filter(|l| !l.is_empty()).unwrap_or_else(|| fallback.to_owned());
        vec![
            ArtifactOption { id: "yes".into(), label: label("yes_label", "Yes").into(), ..ArtifactOption::default() },
            ArtifactOption { id: "no".into(), label: label("no_label", "No").into(), ..ArtifactOption::default() },
        ]
    } else {
        let recommended = text(decision, "recommended_option_id");
        decision
            .get("options")
            .and_then(Value::as_array)
            .map(|options| {
                options
                    .iter()
                    .map(|o| {
                        let (id, label) = (text(o, "id"), text(o, "label"));
                        let recommended = !recommended.is_empty() && id == recommended && !label.to_lowercase().contains("recommended");
                        ArtifactOption { id: id.into(), label: label.into(), detail: text(o, "description").into(), recommended }
                    })
                    .collect()
            })
            .unwrap_or_default()
    };
    item.pending = status == "pending";
    item.chosen = -1;
    item.allow_custom = !item.approval && decision.get("allow_custom_text").and_then(Value::as_bool) == Some(true);
    // A question keeps its options once answered, the answer marked, so
    // its card keeps its height.
    if response == "single_choice" {
        item.options = slint::ModelRc::new(slint::VecModel::from(options.clone()));
    }
    if item.pending {
        let mut meta = Vec::new();
        if !text(decision, "priority_reason").is_empty() {
            meta.push(text(decision, "priority_reason"));
        }
        match text(decision, "response_effort").as_str() {
            "quick" | "" => {}
            "short" => meta.push("About a minute".into()),
            _ => meta.push("Needs a closer look".into()),
        }
        let deadline = chrono::DateTime::parse_from_rfc3339(&text(decision, "deadline_at"));
        if let Ok(deadline) = deadline {
            meta.insert(0, format!("Due {}", deadline.with_timezone(&chrono::Local).format("%b %-d at %H:%M")));
        } else if text(decision, "urgency") == "time_sensitive" {
            meta.push("Time sensitive".into());
        }
        if !item.approval && response != "single_choice" {
            meta.push("Update Clarp to answer this kind of question".into());
        }
        item.meta = meta.join(" · ").into();
        if item.approval {
            item.options = slint::ModelRc::new(slint::VecModel::from(options));
        }
        return;
    }
    let (resolved, ok) = match status.as_str() {
        "answered" => ("Answer saved", true),
        "accepted" => ("Approved", true),
        "rejected" => ("Declined", false),
        "cancelled" => ("Discarded", false),
        "expired" => ("Expired", false),
        _ => ("", false),
    };
    item.resolved = resolved.into();
    item.resolved_ok = ok;
    let answer = decision.get("answer").cloned().unwrap_or(Value::Null);
    let chosen = text(&answer, "option_id");
    item.answer = [text(&answer, "text"), text(&answer, "label")]
        .into_iter()
        .find(|t| !t.is_empty())
        .or_else(|| options.iter().find(|o| !chosen.is_empty() && o.id == chosen.as_str()).map(|o| o.label.to_string()))
        .unwrap_or_default()
        .into();
    if !text(&answer, "text").is_empty() {
        item.chosen = options.len() as i32;
        item.draft = text(&answer, "text").into();
    } else if let Some(index) = options.iter().position(|o| !chosen.is_empty() && o.id == chosen.as_str()) {
        item.chosen = index as i32;
    }
    if decision.get("delivery_pending").and_then(Value::as_bool) == Some(true) {
        item.delivery = "Saved. Waiting to deliver to the agent.".into();
    }
}

/// 1-9 on a card: the answer the keyboard chose.
pub fn choose(app: &App, id: &str, index: i32) {
    let Some(card) = card_item(app, id).filter(|c| c.pending) else { return };
    let options = card.options.row_count();
    if index < 0 || index as usize > options || (index as usize == options && !card.allow_custom) {
        return;
    }
    app.artifact_choices.borrow_mut().insert(id.to_owned(), index);
    // The last number is an answer of one's own: its field takes the keyboard.
    *app.artifact_editing.borrow_mut() = if index as usize == options { id.to_owned() } else { String::new() };
    *app.artifact_cursor.borrow_mut() = id.to_owned();
    app.refresh(&[Change::Updates]);
}

/// The card as the open chat shows it.
fn card_item(app: &App, id: &str) -> Option<ArtifactItem> {
    app.active_messages()?.iter().flat_map(|row| row.artifacts.iter().collect::<Vec<_>>()).find(|a| a.id == id)
}

/// Sends a decision's chosen answer.
pub fn send(app: &App, id: &str) {
    let Some(artifact) = artifact(app, id) else { return };
    let Some(card) = card_item(app, id).filter(|c| c.pending) else { return };
    let decision = artifact.get("decision").cloned().unwrap_or(Value::Null);
    let own = card.allow_custom && card.chosen == card.options.row_count() as i32;
    if own {
        // iOS caps an answer of one's own at 4,000 characters.
        let written: String = app.artifact_drafts.borrow().get(id).map(|d| d.trim().chars().take(4000).collect()).unwrap_or_default();
        if written.is_empty() {
            app.engine.borrow_mut().set_artifact_status(id, "Choose an answer first: write one");
            app.refresh(&[Change::Updates]);
            return;
        }
        app.artifact_editing.borrow_mut().clear();
        // Sent: the keyboard is back on the chat.
        app.focus_transcript();
        app.engine.borrow_mut().send_decision(id, &text(&decision, "decision_id"), "resolve", serde_json::json!({"answer": {"text": written}}), number(&decision, "revision"));
        pump();
        return;
    }
    let Some(option) = usize::try_from(card.chosen).ok().and_then(|i| card.options.row_data(i)) else {
        app.engine.borrow_mut().set_artifact_status(id, "Choose an answer first (1-9)");
        app.refresh(&[Change::Updates]);
        return;
    };
    let body = if card.approval {
        serde_json::json!({"choice": if option.id == "yes" { "accepted" } else { "rejected" }})
    } else {
        serde_json::json!({"answer": {"option_id": option.id.as_str()}})
    };
    app.engine.borrow_mut().send_decision(id, &text(&decision, "decision_id"), "resolve", body, number(&decision, "revision"));
    pump();
}

/// Discards a pending decision (iOS "Discard").
pub fn discard(app: &App, id: &str) {
    let Some(artifact) = artifact(app, id) else { return };
    if !card_item(app, id).is_some_and(|c| c.pending) {
        return;
    }
    let decision = artifact.get("decision").cloned().unwrap_or(Value::Null);
    app.engine.borrow_mut().send_decision(id, &text(&decision, "decision_id"), "dismiss", serde_json::json!({}), number(&decision, "revision"));
    pump();
}

/// Shows the engine's change at once.
fn pump() {
    if let Some(app) = crate::app() {
        crate::pump_now(&app);
    }
}

fn artifact(app: &App, id: &str) -> Option<Value> {
    let found = app.engine.borrow().update_artifacts().iter().find(|a| text(a, "artifact_id") == id).cloned();
    if found.is_none() {
        eprintln!("clarp-slint: no artifact {id}");
    }
    found
}

/// A plan's items and subtasks, depth first.
fn plan_items(plan: &Value) -> Vec<(usize, Value)> {
    fn walk(items: &Value, depth: usize, out: &mut Vec<(usize, Value)>) {
        for item in items.as_array().into_iter().flatten() {
            out.push((depth, item.clone()));
            walk(item.get("subtasks").unwrap_or(&Value::Null), depth + 1, out);
        }
    }
    let mut out = Vec::new();
    walk(plan.get("items").unwrap_or(&Value::Null), 0, &mut out);
    out
}

/// iOS's plan card: items and subtasks completed against the plan's
/// total, and the first one under way.
fn plan_fields(item: &mut ArtifactItem, artifact: &Value) {
    let Some(plan) = artifact.get("plan").filter(|p| p.is_object()) else {
        item.progress_value = -1.0;
        item.current = "Plan details unavailable".into();
        return;
    };
    let items = plan_items(plan);
    let done = items.iter().filter(|(_, i)| text(i, "status") == "completed").count() as i64;
    let total = match number(plan, "total_count") {
        0 => items.len() as i64,
        total => total,
    };
    item.progress_value = done as f32 / total.max(1) as f32;
    item.progress_count = format!("{done}/{total}").into();
    item.current = items.iter().find(|(_, i)| text(i, "status") == "in_progress").map(|(_, i)| text(i, "title")).unwrap_or_default().into();
    item.action = "Open plan".into();
}

/// A research artifact's sources that open: titled https links (iOS shows
/// no others).
fn https_sources(artifact: &Value) -> Vec<(String, String)> {
    artifact
        .get("sources")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|s| (text(s, "title"), text(s, "url")))
        .filter(|(_, url)| url.starts_with("https://"))
        .map(|(title, url)| (if title.is_empty() { url.clone() } else { title }, url))
        .collect()
}

/// The report viewer's model for an artifact without a report body:
/// what its card stands for, in full.
pub fn detail(app: &App, id: &str) -> Option<clarp_core::json::Object> {
    let artifact = app.engine.borrow().update_artifacts().iter().find(|a| text(a, "artifact_id") == id).cloned()?;
    let card = artifact_item(&artifact);
    // Whether the body came from HTML (already read as Markdown here).
    let mut from_html = false;
    let (summary, body) = match text(&artifact, "type").as_str() {
        "plan" => {
            let plan = artifact.get("plan").filter(|p| p.is_object())?;
            let summary = [text(plan, "title"), text(plan, "goal"), format!("{} done", card.progress_count)]
                .into_iter()
                .filter(|p| !p.is_empty())
                .collect::<Vec<_>>()
                .join(" · ");
            // A table: one row per item and subtask, the state beside it.
            let cell = |t: String| t.replace('|', "\\|");
            let mut rows = vec!["| Step | State |".to_owned(), "|---|---|".to_owned()];
            for (depth, i) in plan_items(plan) {
                let (mark, state) = match text(&i, "status").as_str() {
                    "completed" => ("✓", "done"),
                    "in_progress" => ("▶", "under way"),
                    "blocked" | "failed" => ("✕", "blocked"),
                    _ => ("○", "to do"),
                };
                let detail = text(&i, "detail");
                let indent = "↳ ".repeat(depth);
                let state = if detail.is_empty() { state.to_owned() } else { format!("{state} · *{}*", cell(detail)) };
                rows.push(format!("| {indent}{mark} {} | {state} |", cell(text(&i, "title"))));
            }
            let lines = rows;
            (summary, lines.join("\n"))
        }
        "research" => {
            // The body as the Host's report (HTML sanitized, read as
            // Markdown), then the sources as links.
            let report = app.engine.borrow().report_for_artifact(id).unwrap_or_default();
            let body = clarp_core::json::string(&report, "body");
            from_html = clarp_core::json::boolean(&report, "isHtml");
            let mut markdown = if from_html { crate::updates_view::html_markdown(&body) } else { body };
            let sources = https_sources(&artifact);
            if !sources.is_empty() {
                let escape = |t: &str| t.replace('[', "\\[").replace(']', "\\]");
                let list: Vec<String> = sources.iter().map(|(title, url)| format!("- [{}]({url})", escape(title))).collect();
                markdown = format!("{markdown}\n\n## Sources\n\n{}", list.join("\n"));
            }
            (text(&artifact, "summary"), markdown)
        }
        _ => return None,
    };
    serde_json::json!({"artifact_id": id, "title": card.title.as_str(), "summary": summary, "type": card.kind.as_str(), "isHtml": from_html, "converted": true, "kind": card.label.as_str(), "body": body})
        .as_object()
        .cloned()
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

/// Where a card was when it last reported.
struct Report {
    shown: bool,
    height: f32,
    /// The report numbers of its last three reports, newest first.
    seqs: [u64; 3],
}

thread_local! {
    static SHOWN: std::cell::RefCell<(u64, std::collections::HashMap<String, Report>)> = std::cell::RefCell::default();
}

/// A drawn card reports where it is every 150 ms. A row the list dropped
/// stops: it has lapsed once another card has reported three times since
/// (counted in reports, not time, so a busy moment lapses nothing).
fn card_shown(id: &str, top: f32, height: f32, view_top: f32, view_bottom: f32) {
    let shown = top + height > view_top + 4.0 && top < view_bottom - 4.0;
    SHOWN.with(|s| {
        let (seq, reports) = &mut *s.borrow_mut();
        *seq += 1;
        let report = reports.entry(id.to_owned()).or_insert(Report { shown, height, seqs: [0; 3] });
        report.shown = shown;
        report.height = height;
        report.seqs = [*seq, report.seqs[0], report.seqs[1]];
    });
}

/// The heights the cards on screen last reported (for the checks).
pub fn card_heights(app: &App) -> Vec<(String, f32)> {
    let ids = on_screen(app);
    SHOWN.with(|s| ids.into_iter().filter_map(|id| s.borrow().1.get(&id).map(|r| r.height).map(|h| (id, h))).collect())
}

/// The open chat's cards now on screen, top to bottom.
pub fn on_screen(app: &App) -> Vec<String> {
    SHOWN.with(|s| {
        let s = s.borrow();
        let reports = &s.1;
        let lapsed = |report: &Report| reports.values().any(|other| other.seqs[2] > report.seqs[0]);
        card_ids(app).into_iter().filter(|id| reports.get(id).is_some_and(|r| r.shown && !lapsed(r))).collect()
    })
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

/// The card the keyboard is on, while it is on screen in the open chat.
pub fn selected(app: &App) -> Option<String> {
    let cursor = app.artifact_cursor.borrow().clone();
    (!cursor.is_empty() && on_screen(app).contains(&cursor)).then_some(cursor)
}

/// J/K: the next or previous card on screen; from none, J is the topmost
/// and K the lowest. Cards off screen are out of reach (scroll to them).
pub fn step(app: &App, direction: i32) {
    let ids = on_screen(app);
    let Some(last) = ids.len().checked_sub(1) else { return };
    let at = selected(app).and_then(|id| ids.iter().position(|i| *i == id));
    let next = match at {
        None if direction > 0 => 0,
        None => last,
        Some(index) => (index as i64 + i64::from(direction)).clamp(0, last as i64) as usize,
    };
    *app.artifact_cursor.borrow_mut() = ids[next].clone();
    app.refresh(&[Change::Updates]);
}

/// A card's action (Enter on the selected card, or a click).
pub fn open(app: &App, window: &AppWindow, id: &str) {
    let Some(artifact) = artifact(app, id) else { return };
    *app.artifact_cursor.borrow_mut() = id.to_owned();
    match text(&artifact, "type").as_str() {
        "html_form" if !is_report(&artifact) => {
            let version = artifact.get("version").cloned().unwrap_or(Value::Null);
            match crate::form_server::serve(id, version, &text(&artifact, "content")) {
                Ok(url) => crate::open_link(&url),
                Err(error) => {
                    eprintln!("clarp-slint: {error}");
                    app.engine.borrow_mut().set_artifact_status(id, &format!("Not opened: {error}"));
                }
            }
        }
        "decision" | "question" => return send(app, id),
        "plan" if detail(app, id).is_some() => crate::updates_view::open_report(app, window, id),
        _ if app.engine.borrow().report_for_artifact(id).is_some() => crate::updates_view::open_report(app, window, id),
        // Nothing to open: Enter only selects.
        _ => {}
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
    bridge.on_link_clicked(|url| crate::open_link(&url));
    bridge.on_card_shown(|id, top, height, view_top, view_bottom| card_shown(&id, top, height, view_top, view_bottom));
    bridge.on_choose(|id, index| {
        if let Some(app) = crate::app() {
            choose(&app, &id, index);
        }
    });
    bridge.on_send(|id| {
        if let Some(app) = crate::app() {
            send(&app, &id);
        }
    });
    bridge.on_draft_edited(|id, text| {
        if let Some(app) = crate::app() {
            app.artifact_drafts.borrow_mut().insert(id.to_string(), text.to_string());
        }
    });
    bridge.on_editing_changed(|id, editing| {
        if let Some(app) = crate::app() {
            if !editing && *app.artifact_editing.borrow() == id.as_str() {
                // Left without sending: the field stays with its draft, but
                // no longer takes the keyboard (the card updates in place).
                app.artifact_editing.borrow_mut().clear();
                app.refresh(&[Change::Updates]);
            }
        }
    });
    bridge.on_discard(|id| {
        if let Some(app) = crate::app() {
            discard(&app, &id);
        }
    });
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
            // A selected card scrolled away is no longer selected.
            if let Some(app) = crate::app() {
                let cursor = app.artifact_cursor.borrow().clone();
                if !cursor.is_empty() && selected(&app).is_none() && app.artifact_editing.borrow().is_empty() {
                    app.artifact_cursor.borrow_mut().clear();
                    app.refresh(&[Change::Updates]);
                }
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
