//! The Updates surface (UpdatesPanel.qml) and the report viewer
//! (ReportView.qml): engine state turned into their Slint models, and what
//! their buttons do. An agent's processes are `processes_view`.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use clarp_core::json::{self, Object};
use clarp_engine::Change;
use serde_json::Value;
use slint::{ModelRc, VecModel};

use crate::{App, AppWindow, ArtifactView, AttentionView, JobView, MessageBlock, commands, pump_now};

thread_local! {
    /// The artifact the report viewer shows ("" for none).
    static REPORT: RefCell<String> = const { RefCell::new(String::new()) };
    /// Reloads the updates every 10 s while they show (as the QML Timer).
    static POLL: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
}

fn text(object: &Object, key: &str) -> String {
    json::js_string(object.get(key))
}

/// The first of `keys` that is set, else `fallback`.
fn first(object: &Object, keys: &[&str], fallback: &str) -> String {
    keys.iter().map(|k| text(object, k)).find(|v| !v.is_empty()).unwrap_or_else(|| fallback.to_owned())
}

fn active_job(status: &str) -> bool {
    matches!(status, "queued" | "running" | "active")
}

fn attention_view(engine: &clarp_engine::Engine, item: &Object) -> AttentionView {
    let id = text(item, "decision_id");
    AttentionView {
        pending: engine.update_action_pending("decision", &id),
        title: first(item, &["title"], "Decision").into(),
        question: first(item, &["question", "summary"], "").into(),
        context: text(item, "context").into(),
        meta: format!("{}  ·  {}", text(item, "agent_name"), text(item, "session")).into(),
        no_label: first(item, &["no_label"], "No").into(),
        yes_label: first(item, &["yes_label"], "Yes").into(),
        revision: json::js_number(item.get("revision")) as i32,
        id: id.into(),
    }
}

fn job_view(engine: &clarp_engine::Engine, job: &Object) -> JobView {
    let id = text(job, "job_id");
    let status = text(job, "status");
    JobView {
        pending: engine.update_action_pending("job", &id),
        title: first(job, &["title", "kind"], "Background job").into(),
        detail: text(job, "detail").into(),
        active: active_job(&status),
        failed: status == "failed",
        status: status.to_uppercase().into(),
        progress: engine.background_job_progress(job) as f32,
        can_cancel: job.get("can_cancel") != Some(&Value::Bool(false)),
        id: id.into(),
    }
}

/// ArtifactSummaryCard.qml.
fn artifact_view(engine: &clarp_engine::Engine, artifact: &Object) -> ArtifactView {
    let kind = first(artifact, &["type"], "item");
    let outcome = first(artifact, &["conclusion", "status"], "unknown");
    let plan = json::object(artifact, "plan");
    let count = |object: &Object, key: &str| json::js_number(object.get(key)).max(0.0) as i64;
    let (total, completed) =
        if kind == "plan" { (count(&plan, "total_count"), count(&plan, "completed_count")) } else { (count(artifact, "total_steps"), count(artifact, "completed_steps")) };
    let progress = match kind.as_str() {
        "plan" | "workflow_run" if total > 0 => format!("{completed} / {total} completed"),
        "plan" | "workflow_run" if outcome == "active" => "In progress".to_owned(),
        _ => String::new(),
    };
    let countdown = if kind == "countdown" {
        let zone = text(artifact, "time_zone");
        let at = text(artifact, "target_at");
        if zone.is_empty() { at } else { format!("{at} · {zone}") }
    } else {
        String::new()
    };
    ArtifactView {
        id: text(artifact, "artifact_id").into(),
        kind: kind.replace('_', " ").to_uppercase().into(),
        failed: matches!(outcome.as_str(), "failed" | "failure" | "timed_out" | "action_required"),
        outcome: outcome.into(),
        title: first(artifact, &["file_name", "title"], "Artifact").into(),
        summary: text(artifact, "summary").into(),
        progress: progress.into(),
        countdown: countdown.into(),
        readable: engine.artifact_is_viewable_report(artifact),
        session: text(artifact, "session").into(),
        form: kind == "html_form",
    }
}

fn model<T: Clone + 'static>(rows: Vec<T>) -> ModelRc<T> {
    ModelRc::new(VecModel::from(rows))
}

fn objects(values: &[Value]) -> impl Iterator<Item = &Object> {
    values.iter().filter_map(Value::as_object)
}

/// The artifacts the Updates surface lists. CI runs stay in their chat: the
/// owner cannot act on them here, and a blocked agent asks a question instead.
fn updates_listed(values: &[Value]) -> impl Iterator<Item = &Object> {
    objects(values).filter(|artifact| text(artifact, "type") != "workflow_run")
}

/// Rebuilds the surface's lists from the engine.
pub fn show(app: &App, window: &AppWindow) {
    let engine = app.engine.borrow();
    window.set_update_attention(model(objects(engine.attention_items()).map(|i| attention_view(&engine, i)).collect()));
    window.set_update_jobs(model(objects(engine.background_jobs()).map(|j| job_view(&engine, j)).collect()));
    window.set_update_artifacts(model(updates_listed(engine.update_artifacts()).map(|a| artifact_view(&engine, a)).collect()));
    window.set_updates_loading(engine.updates_loading());
    window.set_updates_error(engine.updates_error().into());
    window.set_attention(engine.attention_count() as i32);
}

/// Ctrl+2 or the rail: shows the surface, reloads it and keeps it fresh.
pub fn open(app: &App, window: &AppWindow) {
    window.set_surface("updates".into());
    app.engine.borrow_mut().load_updates();
    window.invoke_focus_updates();
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, Duration::from_secs(10), || {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        if window.get_surface() != "updates" {
            POLL.with(|p| p.borrow_mut().take());
            return;
        }
        app.engine.borrow_mut().load_updates();
        pump_now(&app);
    });
    POLL.with(|p| *p.borrow_mut() = Some(timer));
}

/// What the engine's changes mean here: the lists, the rail's badge, an
/// open report and an open processes panel.
pub fn refresh(app: &App, window: &AppWindow, changes: &[Change]) {
    if changes.contains(&Change::Updates) {
        show(app, window);
        if *app.overlay.borrow() == "report" {
            show_report(app, window);
        }
        commands::show_hints(app, window);
    }
    let processes = changes.iter().any(|c| matches!(c, Change::Processes | Change::Roster | Change::Updates));
    if processes {
        crate::processes_view::show(app, window);
    }
}

/// A panel's "Open chat": the chat, on the chats surface, ready to type.
pub fn open_chat(app: &Rc<App>, window: &AppWindow, session: &str) {
    if session.is_empty() {
        return;
    }
    app.engine.borrow_mut().select(session);
    window.set_surface("chats".into());
    pump_now(app);
    app.focus_composer();
    commands::show_hints(app, window);
}

/// Ctrl+J / N: the next chat that wants the user.
pub fn next_attention(app: &Rc<App>, window: &AppWindow) {
    let session = app.engine.borrow().next_attention_session();
    if !session.is_empty() {
        open_chat(app, window, &session);
    }
}

// ---- the report viewer ------------------------------------------------------

pub fn open_report(app: &App, window: &AppWindow, artifact_id: &str) {
    REPORT.with(|r| *r.borrow_mut() = artifact_id.to_owned());
    commands::open_overlay(app, window, "report");
    show_report(app, window);
    // The keyboard reads it (arrows, Page keys, 1-9 for its links).
    window.invoke_focus_report();
}

thread_local! {
    /// The open report's links, in order (1-9 open them).
    static REPORT_LINKS: std::cell::RefCell<Vec<String>> = const { std::cell::RefCell::new(Vec::new()) };
}

/// The web links of a Markdown body, in order, once each.
fn links(markdown: &str) -> Vec<String> {
    let mut found: Vec<String> = Vec::new();
    for (at, _) in markdown.match_indices("](") {
        let rest = &markdown[at + 2..];
        let Some(end) = rest.find(')') else { continue };
        let url = rest[..end].trim();
        if (url.starts_with("https://") || url.starts_with("http://")) && !found.iter().any(|f| f == url) {
            found.push(url.to_owned());
        }
    }
    found
}

/// 1-9 in the report: opens its link.
pub fn open_report_link(index: i32) {
    let url = REPORT_LINKS.with(|l| usize::try_from(index).ok().and_then(|i| l.borrow().get(i).cloned()));
    if let Some(url) = url {
        crate::open_link(&url);
    }
}

/// The report's title, summary and body (Markdown blocks) for the viewer.
fn show_report(app: &App, window: &AppWindow) {
    let id = REPORT.with(|r| r.borrow().clone());
    // What a card stands for (a plan, research with its sources...),
    // else the artifact's report body.
    let report = crate::artifacts_view::detail(app, &id).or_else(|| app.engine.borrow().report_for_artifact(&id)).unwrap_or_default();
    let html = json::boolean(&report, "isHtml");
    let body = text(&report, "body");
    let markdown = if html && !json::boolean(&report, "converted") { html_markdown(&body) } else { body };
    let blocks: Vec<MessageBlock> = clarp_engine::blocks::blocks(&markdown).iter().map(|b| crate::view::message_block(b, false)).collect();
    let found = links(&markdown);
    window.set_report_links(found.len() as i32);
    REPORT_LINKS.with(|l| *l.borrow_mut() = found);
    window.set_report_title(text(&report, "title").into());
    window.set_report_summary(text(&report, "summary").into());
    window.set_report_html(html);
    window.set_report_kind(text(&report, "kind").into());
    window.set_report_blocks(model(blocks));
}

/// Sanitized report HTML as Markdown, which Slint can draw: headings,
/// paragraphs, lists, emphasis, code and links survive; scripts, styles and
/// everything else become plain text.
pub fn html_markdown(html: &str) -> String {
    fn attribute(tag: &str, name: &str) -> String {
        let lower = tag.to_ascii_lowercase();
        let Some(at) = lower.find(&format!("{name}=")) else { return String::new() };
        let value = tag[at + name.len() + 1..].trim_start();
        match value.chars().next() {
            Some(quote @ ('"' | '\'')) => value[1..].split(quote).next().unwrap_or_default().to_owned(),
            _ => value.split(|c: char| c.is_whitespace() || c == '>').next().unwrap_or_default().to_owned(),
        }
    }
    fn decode(text: &str) -> String {
        let mut out = String::new();
        let mut rest = text;
        while let Some(at) = rest.find('&') {
            out.push_str(&rest[..at]);
            let tail = &rest[at..];
            let end = tail.find(';').filter(|&e| e <= 10);
            let entity = end.map(|e| &tail[1..e]).unwrap_or_default();
            let decoded = match entity {
                "amp" => Some('&'),
                "lt" => Some('<'),
                "gt" => Some('>'),
                "quot" => Some('"'),
                "apos" | "#39" => Some('\''),
                "nbsp" => Some(' '),
                _ => entity
                    .strip_prefix("#x")
                    .or_else(|| entity.strip_prefix("#X"))
                    .and_then(|h| u32::from_str_radix(h, 16).ok())
                    .or_else(|| entity.strip_prefix('#').and_then(|d| d.parse().ok()))
                    .and_then(char::from_u32),
            };
            match (decoded, end) {
                (Some(c), Some(e)) => {
                    out.push(c);
                    rest = &tail[e + 1..];
                }
                _ => {
                    out.push('&');
                    rest = &tail[1..];
                }
            }
        }
        out + rest
    }
    let mut out = String::new();
    let mut rest = html;
    let mut skip: Option<String> = None;
    let mut pre = false;
    let mut links: Vec<String> = Vec::new();
    loop {
        let (text, next) = match rest.find('<') {
            Some(at) => (&rest[..at], Some(at)),
            None => (rest, None),
        };
        if skip.is_none() {
            let text = decode(text);
            if pre {
                out.push_str(&text);
            } else {
                let collapsed: String = text.split_whitespace().collect::<Vec<_>>().join(" ");
                if !collapsed.is_empty() {
                    if text.starts_with(char::is_whitespace) && !out.ends_with(char::is_whitespace) {
                        out.push(' ');
                    }
                    out.push_str(&collapsed);
                    if text.ends_with(char::is_whitespace) {
                        out.push(' ');
                    }
                }
            }
        }
        let Some(at) = next else { break };
        let tail = &rest[at..];
        if let Some(comment) = tail.strip_prefix("<!--") {
            rest = comment.find("-->").map_or("", |e| &comment[e + 3..]);
            continue;
        }
        let Some(end) = tail.find('>') else { break };
        let tag = &tail[1..end];
        rest = &tail[end + 1..];
        let closing = tag.starts_with('/');
        let name = tag.trim_start_matches('/').split(|c: char| c.is_whitespace() || c == '/').next().unwrap_or_default().to_ascii_lowercase();
        if let Some(skipped) = &skip {
            if closing && name == *skipped {
                skip = None;
            }
            continue;
        }
        match name.as_str() {
            "script" | "style" | "head" | "title" | "template" if !closing => skip = Some(name),
            "h1" | "h2" | "h3" | "h4" | "h5" | "h6" => {
                out.push_str("\n\n");
                if !closing {
                    let level = name[1..].parse::<usize>().unwrap_or(1);
                    out.push_str(&"#".repeat(level));
                    out.push(' ');
                }
            }
            "pre" => {
                pre = !closing;
                out.push_str(if closing { "\n```\n\n" } else { "\n\n```\n" });
            }
            "p" | "div" | "section" | "article" | "header" | "footer" | "main" | "table" | "ul" | "ol" | "blockquote" | "hr" => out.push_str("\n\n"),
            "br" | "tr" => out.push('\n'),
            "li" if !closing => out.push_str("\n- "),
            "td" | "th" if !closing => out.push(' '),
            "strong" | "b" => out.push_str("**"),
            "em" | "i" => out.push('*'),
            "code" if !pre => out.push('`'),
            "a" if !closing => {
                let href = attribute(tag, "href");
                if !href.is_empty() {
                    out.push('[');
                }
                links.push(href);
            }
            "a" => {
                if let Some(href) = links.pop().filter(|h| !h.is_empty()) {
                    out.push_str(&format!("]({href})"));
                }
            }
            _ => {}
        }
    }
    // Tidy the lines: no trailing spaces, at most one blank line in a row.
    let mut tidy = String::new();
    let mut blank = true;
    let mut fenced = false;
    for line in out.lines() {
        if line.trim_start().starts_with("```") {
            fenced = !fenced;
        }
        let line = if fenced { line.trim_end() } else { line.trim() };
        if line.is_empty() {
            if !blank {
                tidy.push('\n');
            }
            blank = true;
            continue;
        }
        blank = false;
        tidy.push_str(line);
        tidy.push('\n');
    }
    tidy.trim_end().to_owned()
}

#[cfg(test)]
mod tests {
    use super::{html_markdown, text, updates_listed};
    use serde_json::json;

    #[test]
    fn ci_runs_never_reach_the_updates_list() {
        let values = vec![
            json!({"artifact_id": "doc", "type": "document"}),
            json!({"artifact_id": "ci", "type": "workflow_run", "status": "completed", "conclusion": "failure"}),
            json!({"artifact_id": "page", "type": "html_report"}),
        ];
        let listed: Vec<String> = updates_listed(&values).map(|a| text(a, "artifact_id")).collect();
        assert_eq!(listed, ["doc", "page"]);
    }

    #[test]
    fn report_html_reads_as_markdown() {
        let html = "<!doctype html><html><head><title>T</title><style>h1{}</style></head><body>\
            <h1>Page</h1><p>Some <b>bold</b> &amp; <a href=\"https://example.com\">a link</a>.</p>\
            <!-- note --><ul><li>one</li><li>two</li></ul><script>alert(1)</script><pre>a  b\n c</pre><img src=\"\"></body></html>";
        assert_eq!(html_markdown(html), "# Page\n\nSome **bold** & [a link](https://example.com).\n\n- one\n- two\n\n```\na  b\n c\n```");
    }
}
