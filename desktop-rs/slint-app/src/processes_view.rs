//! An agent's background work (ProcessPopover.qml; the iOS app's
//! AgentProcessesSheet and ProcessDetailView): its jobs and running helpers,
//! a job's output (its log tail, read again every two seconds while it
//! shows) and a stop for either after a confirm. The explorer's badge opens
//! it under the row; a key, Ctrl+K or the vim leader opens it for the
//! selected agent. Opened from Updates it lists every job the Host reports.
//!
//! The keyboard: j/k or the arrows move, Enter opens a job's output or a
//! helper's chat, x or Delete stops (y or Enter confirms), Escape goes back
//! and then closes, giving the keyboard back where it was.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use clarp_core::json::{self, Object};
use serde_json::Value;
use slint::{Model, ModelRc, SharedString, VecModel};

use crate::{App, AppWindow, ProcessHelper, ProcessJob, commands};

pub const OVERLAY: &str = "processes";

/// Log lines the output shows at once.
const LINES: usize = 18;
/// Seconds between reads of an open job's output (iOS polls as often).
const OUTPUT_EVERY: u32 = 2;

/// What a confirm stops.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Target {
    Job { id: String, title: String, generation: Option<i64> },
    Helper { session: String, name: String },
}

/// One row of the list, jobs before helpers.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Row {
    Job(String),
    Helper(String),
}

#[derive(Debug, Default)]
struct Panel {
    /// The agent whose work it lists; "" lists every job (Updates).
    session: String,
    rows: Vec<Row>,
    cursor: usize,
    /// The job whose output shows ("" for the list).
    output: String,
    /// Opened on a job's output (Updates): going back closes it.
    direct: bool,
    /// Log lines scrolled back from the newest.
    back: usize,
    confirm: Option<Target>,
    /// What the last stop of a helper did (jobs keep theirs in the engine).
    note: String,
    /// The keyboard map's state it was opened from.
    origin: &'static str,
    ticks: u32,
}

thread_local! {
    static PANEL: RefCell<Panel> = RefCell::new(Panel::default());
    /// Ticks elapsed times and reads an open output while the panel shows.
    static TICK: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
}

fn text(object: &Object, key: &str) -> String {
    json::js_string(object.get(key))
}

fn model<T: Clone + 'static>(rows: Vec<T>) -> ModelRc<T> {
    ModelRc::new(VecModel::from(rows))
}

pub fn is_open(app: &App) -> bool {
    *app.overlay.borrow() == OVERLAY
}

/// The explorer badge: the list under the indicator at (`x`, `y`).
pub fn open_at(app: &App, window: &AppWindow, session: &str, x: f32, y: f32) {
    window.set_process_centered(false);
    window.set_process_x(x);
    window.set_process_y(y);
    open(app, window, session, "");
    // A click gives the keyboard back to the composer, as it always has.
    PANEL.with(|p| p.borrow_mut().origin = "composer");
}

/// A key, Ctrl+K or the vim leader: the selected agent's work, or every
/// job on Updates. Again closes it.
pub fn toggle(app: &App, window: &AppWindow) {
    if is_open(app) {
        commands::close_overlay(app, window);
        return;
    }
    window.set_process_centered(true);
    // In the explorer, the row the keyboard is on.
    let cursor = window.get_sidebar_cursor().to_string();
    let session = if window.get_surface() == "updates" {
        String::new()
    } else if commands::context(app, window) == "sidebar" && !cursor.is_empty() {
        cursor
    } else {
        app.engine.borrow().selected_session().to_owned()
    };
    if !session.is_empty() || window.get_surface() == "updates" {
        open(app, window, &session, "");
    }
}

/// An Updates job's Output or Cancel: that job, with every job behind it.
pub fn open_job(app: &App, window: &AppWindow, job_id: &str, stop: bool) {
    window.set_process_centered(true);
    open(app, window, "", job_id);
    if stop {
        ask_stop(app, window);
    }
}

fn open(app: &App, window: &AppWindow, session: &str, job: &str) {
    let origin = commands::context(app, window);
    PANEL.with(|p| {
        *p.borrow_mut() = Panel { session: session.to_owned(), output: job.to_owned(), direct: !job.is_empty(), origin, ..Panel::default() };
    });
    commands::open_overlay(app, window, OVERLAY);
    // Titles and heartbeats come from the job list: read it now so the list
    // is never older than the counts beside the badge.
    app.engine.borrow_mut().load_updates();
    if !job.is_empty() {
        app.engine.borrow_mut().load_job_detail(job);
    }
    show(app, window);
    window.invoke_focus_processes();
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, Duration::from_secs(1), || {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        if !is_open(&app) {
            TICK.with(|t| t.borrow_mut().take());
            return;
        }
        let due = PANEL.with(|p| {
            let p = &mut *p.borrow_mut();
            p.ticks += 1;
            (!p.output.is_empty() && p.ticks % OUTPUT_EVERY == 0).then(|| p.output.clone())
        });
        if let Some(job) = due {
            app.engine.borrow_mut().load_job_detail(&job);
        }
        show(&app, &window);
    });
    TICK.with(|t| *t.borrow_mut() = Some(timer));
}

/// Ctrl+K opened it: the keyboard goes back where it was before Ctrl+K.
pub fn set_origin(composer: bool) {
    PANEL.with(|p| p.borrow_mut().origin = if composer { "composer" } else { "pane" });
}

/// The panel closed on the chats: the keyboard goes back to the explorer,
/// the chat or the composer it was opened from. False when another place
/// takes it.
pub fn give_back(app: &App, window: &AppWindow) -> bool {
    if window.get_surface() != "chats" {
        return false;
    }
    match PANEL.with(|p| p.borrow().origin) {
        "sidebar" | "search" => window.invoke_focus_sidebar(),
        "pane" => app.focus_transcript(),
        _ => app.focus_composer(),
    }
    true
}

/// The job as the Host last listed it, else as its detail has it.
fn listed_job(engine: &clarp_engine::Engine, id: &str) -> Option<Object> {
    engine
        .background_jobs()
        .iter()
        .filter_map(Value::as_object)
        .find(|j| text(j, "job_id") == id)
        .cloned()
        .or_else(|| engine.job_detail(id).and_then(|d| d.as_ref().ok()).map(|d| json::object(d, "job")).filter(|j| !j.is_empty()))
}

fn active(status: &str) -> bool {
    matches!(status, "queued" | "running" | "active")
}

fn stoppable(job: &Object) -> bool {
    active(&text(job, "status")) && job.get("can_cancel") != Some(&Value::Bool(false))
}

fn job_title(job: &Object) -> String {
    let title = text(job, "title");
    if !title.trim().is_empty() {
        return title;
    }
    if text(job, "kind") == "sub-agent" { "Sub-agent".into() } else { "Background job".into() }
}

/// "running 3m · ♥ 8s ago", "cancelled after 14m".
fn timing(job: &Object, now: i64) -> String {
    let status = text(job, "status");
    let started = json::integer(job, "started_at");
    let heartbeat = json::integer(job, "heartbeat_at");
    let mut parts = vec![if status.is_empty() { "unknown".to_owned() } else { status.clone() }];
    if started > 0 {
        let end = if active(&status) { now } else { json::integer(job, "finished_at").max(json::integer(job, "updated_at")).max(started) };
        let elapsed = clarp_core::time_format::compact_duration(end - started);
        parts[0] = if active(&status) { format!("{status} {elapsed}") } else { format!("{status} after {elapsed}") };
    }
    if active(&status) && heartbeat > 0 {
        parts.push(format!("♥ {} ago", clarp_core::time_format::compact_duration(now - heartbeat)));
    }
    parts.join(" · ")
}

/// ProcessDetailText.logUnavailable.
fn log_unavailable(reason: &str) -> String {
    match reason {
        "no_log" => "This process didn't register a log.".into(),
        "outside_allowed_roots" | "not_absolute" => "Its log is outside the folders Clarp may read.".into(),
        "missing" => "Its log file is gone.".into(),
        "forbidden" => "This device may not read process logs.".into(),
        "not_a_regular_file" | "unreadable" => "Its log can't be read.".into(),
        other => format!("No log available ({other})."),
    }
}

/// The lines of `log` that show, `back` lines up from the newest, and how
/// far back it can go.
fn tail(log: &str, back: usize) -> (Vec<String>, usize) {
    let lines: Vec<&str> = log.trim_end_matches('\n').lines().collect();
    let most = lines.len().saturating_sub(LINES);
    let back = back.min(most);
    let end = lines.len() - back;
    (lines[end.saturating_sub(LINES)..end].iter().map(|l| l.replace('\t', "    ")).collect(), most)
}

/// "10:02:03  running" for each of the timeline's changes, newest last.
fn timeline(detail: &Object) -> Vec<String> {
    json::array(detail, "timeline")
        .iter()
        .filter_map(Value::as_object)
        .map(|entry| {
            let at = chrono::DateTime::from_timestamp_millis(json::integer(entry, "observed_at"))
                .map(|t| t.with_timezone(&chrono::Local).format("%H:%M:%S").to_string())
                .unwrap_or_default();
            let what = [text(entry, "status"), text(entry, "note")].into_iter().filter(|s| !s.is_empty()).collect::<Vec<_>>().join(" · ");
            format!("{at}  {what}")
        })
        .collect()
}

fn job_row(engine: &clarp_engine::Engine, job: &Object, now: i64, all: bool) -> ProcessJob {
    let id = text(job, "job_id");
    let sub_agent = text(job, "kind") == "sub-agent";
    let kind = if sub_agent { "sub-agent".to_owned() } else { text(job, "kind") };
    let status = text(job, "status");
    let owner = if all { engine.chat_name(&text(job, "session")) } else { String::new() };
    let state = if status == "queued" || (all && !status.is_empty()) { status.clone() } else { String::new() };
    let detail: Vec<String> = [owner, kind, state, text(job, "detail")].into_iter().filter(|p| !p.is_empty()).collect();
    let started = json::integer(job, "started_at");
    let heartbeat = json::integer(job, "heartbeat_at");
    ProcessJob {
        title: job_title(job).into(),
        detail: detail.join(" · ").into(),
        elapsed: if started > 0 && active(&status) { clarp_core::time_format::compact_duration(now - started).into() } else { SharedString::new() },
        heartbeat: if heartbeat > 0 && active(&status) { format!("{} ago", clarp_core::time_format::compact_duration(now - heartbeat)).into() } else { SharedString::new() },
        sub_agent,
        can_stop: stoppable(job),
        outcome: engine.job_outcome(&id).into(),
        id: id.into(),
    }
}

/// Rebuilds the panel from the engine.
pub fn show(app: &App, window: &AppWindow) {
    if !is_open(app) {
        return;
    }
    let engine = app.engine.borrow();
    let now = chrono::Utc::now().timestamp_millis();
    let (session, output) = PANEL.with(|p| (p.borrow().session.clone(), p.borrow().output.clone()));
    let (jobs, helpers, name, count, empty) = if session.is_empty() {
        let jobs: Vec<ProcessJob> = engine.background_jobs().iter().filter_map(Value::as_object).map(|j| job_row(&engine, j, now, true)).collect();
        let running = engine.background_jobs().iter().filter_map(Value::as_object).filter(|j| active(&text(j, "status"))).count();
        let count = if running == 0 { "nothing running".to_owned() } else { format!("{running} running") };
        let empty = if jobs.is_empty() { "No background jobs.".to_owned() } else { String::new() };
        (jobs, Vec::new(), "Background jobs".to_owned(), count, empty)
    } else {
        let processes = engine.agent_processes(&session).unwrap_or_default();
        let jobs: Vec<ProcessJob> = json::array(&processes, "jobs")
            .iter()
            .filter_map(Value::as_object)
            .map(|row| {
                let id = text(row, "jobId");
                let job = listed_job(&engine, &id).unwrap_or_else(|| row.clone());
                let mut view = job_row(&engine, &job, now, false);
                // The roster's own words for elapsed and heartbeat.
                view.elapsed = text(row, "elapsed").into();
                view.heartbeat = text(row, "heartbeat").into();
                view
            })
            .collect();
        let helpers: Vec<ProcessHelper> = json::array(&processes, "helpers")
            .iter()
            .filter_map(Value::as_object)
            .map(|helper| {
                let status = text(helper, "statusText");
                ProcessHelper {
                    session: text(helper, "session").into(),
                    name: text(helper, "name").into(),
                    detail: if status.is_empty() { "helper".into() } else { format!("helper · {status}").into() },
                }
            })
            .collect();
        let count = |key: &str| json::integer(&processes, key).max(0) as usize;
        let unlisted = count("jobCount").saturating_sub(jobs.len()) + count("runningChildren").saturating_sub(helpers.len());
        let empty = if jobs.is_empty() && helpers.is_empty() && unlisted == 0 {
            "Nothing running.".to_owned()
        } else if unlisted > 0 {
            format!("{unlisted} more reported by the Host, details not loaded yet.")
        } else {
            String::new()
        };
        let total = count("total");
        let count = if total == 0 { "nothing running".to_owned() } else { format!("{total} running") };
        (jobs, helpers, engine.chat_name(&session), count, empty)
    };
    let rows: Vec<Row> = jobs.iter().map(|j| Row::Job(j.id.to_string())).chain(helpers.iter().map(|h| Row::Helper(h.session.to_string()))).collect();
    let (cursor, confirm, note, back) = PANEL.with(|p| {
        let p = &mut *p.borrow_mut();
        // The cursor stays on its row while others come and go.
        let current = p.rows.get(p.cursor).cloned();
        p.cursor = current.and_then(|c| rows.iter().position(|r| *r == c)).unwrap_or(p.cursor).min(rows.len().saturating_sub(1));
        p.rows = rows.clone();
        (p.cursor, p.confirm.clone(), p.note.clone(), p.back)
    });
    window.set_process_name(name.into());
    window.set_process_count(count.into());
    window.set_process_jobs(model(jobs));
    window.set_process_helpers(model(helpers));
    window.set_process_note((if note.is_empty() { empty } else { note }).into());
    window.set_process_cursor(cursor as i32);
    window.set_process_mode(if output.is_empty() { "list".into() } else { "output".into() });
    window.set_process_confirm(match &confirm {
        Some(Target::Job { title, .. }) => format!("Stop {title}? Only this run stops; a newer one is left alone.").into(),
        Some(Target::Helper { name, .. }) => format!("Stop {name}? Its current turn ends; the chat stays.").into(),
        None => SharedString::new(),
    });
    if !output.is_empty() {
        let job = listed_job(&engine, &output);
        let detail = engine.job_detail(&output);
        let log = detail.and_then(|d| d.as_ref().ok()).map(|d| json::object(d, "log")).unwrap_or_default();
        let (lines, most) = tail(&text(&log, "text"), back);
        PANEL.with(|p| p.borrow_mut().back = back.min(most));
        let note = match detail {
            None => "Reading its output…".to_owned(),
            Some(Err(error)) => format!("Couldn't read its output: {error}"),
            Some(Ok(_)) if json::boolean(&log, "available") && lines.is_empty() => "Its log is empty so far.".to_owned(),
            Some(Ok(_)) if json::boolean(&log, "available") => String::new(),
            Some(Ok(_)) => log_unavailable(&text(&log, "reason")),
        };
        let progress = detail.and_then(|d| d.as_ref().ok()).map(|d| text(&json::object(d, "progress"), "text")).unwrap_or_default();
        let events = detail.and_then(|d| d.as_ref().ok()).map(timeline).unwrap_or_default();
        let shown = events.len().saturating_sub(4);
        window.set_process_output_title(job.as_ref().map(job_title).unwrap_or_else(|| "Background job".into()).into());
        window.set_process_output_status(job.as_ref().map(|j| timing(j, now)).unwrap_or_default().into());
        window.set_process_output_progress(progress.into());
        window.set_process_output_lines(model(lines.into_iter().map(SharedString::from).collect()));
        window.set_process_output_back(back.min(most) as i32);
        window.set_process_output_events(model(events[shown..].iter().map(|e| SharedString::from(e.as_str())).collect()));
        window.set_process_output_note(note.into());
        window.set_process_output_outcome(engine.job_outcome(&output).into());
        window.set_process_output_stoppable(job.as_ref().is_some_and(stoppable));
        window.set_process_output_owner(job.as_ref().map(|j| engine.chat_name(&text(j, "session"))).unwrap_or_default().into());
    }
    drop(engine);
    window.set_process_hint(hint(window).into());
}

/// The panel's hint line: the keys that work right now.
fn hint(window: &AppWindow) -> String {
    let (output, direct, confirm) = PANEL.with(|p| {
        let p = p.borrow();
        (!p.output.is_empty(), p.direct, p.confirm.is_some())
    });
    if confirm {
        return "y/Enter stop · n/Esc keep running".into();
    }
    if output {
        let stop = if window.get_process_output_stoppable() { " · x stop" } else { "" };
        let back = if direct { "Esc close" } else { "Esc back" };
        return format!("j/k scroll · End latest{stop} · c chat · {back}");
    }
    let rows = window.get_process_jobs().row_count() + window.get_process_helpers().row_count();
    if rows == 0 {
        return "Esc close".into();
    }
    "j/k move · Enter open · x stop · Esc close".into()
}

/// The hints the shortcut bar shows while the panel has the keyboard.
pub fn hints() -> Vec<crate::Hint> {
    let (output, confirm) = PANEL.with(|p| (!p.borrow().output.is_empty(), p.borrow().confirm.is_some()));
    let pairs: &[(&str, &str)] = if confirm {
        &[("Y", "Stop"), ("N", "Keep running")]
    } else if output {
        &[("J/K", "Scroll"), ("End", "Latest"), ("X", "Stop"), ("C", "Chat"), ("Esc", "Back")]
    } else {
        &[("J/K", "Move"), ("Enter", "Open"), ("X", "Stop"), ("Esc", "Close")]
    };
    pairs.iter().map(|(keys, label)| crate::Hint { keys: (*keys).into(), label: (*label).into() }).collect()
}

/// A key while the panel shows; false for one it leaves alone.
pub fn key(app: &Rc<App>, window: &AppWindow, chord: &str) -> bool {
    let (output, confirm) = PANEL.with(|p| (!p.borrow().output.is_empty(), p.borrow().confirm.is_some()));
    let used = if confirm {
        match chord {
            "Return" | "Y" => {
                confirm_stop(app, window);
                crate::pump_now(app);
                true
            }
            "Escape" | "N" => {
                PANEL.with(|p| p.borrow_mut().confirm = None);
                true
            }
            _ => true,
        }
    } else if output {
        match chord {
            "Escape" | "Backspace" | "Left" | "H" => {
                back(app, window);
                true
            }
            "Q" => {
                commands::close_overlay(app, window);
                return true;
            }
            "X" | "Delete" => {
                ask_stop(app, window);
                true
            }
            "C" => {
                open_owner(app, window);
                return true;
            }
            "Up" | "K" => scroll(1),
            "Down" | "J" => scroll(-1),
            "PageUp" => scroll(LINES as i64),
            "PageDown" => scroll(-(LINES as i64)),
            "Home" => scroll(i64::MAX / 2),
            "End" | "Shift+G" => scroll(i64::MIN / 2),
            _ => false,
        }
    } else {
        match chord {
            "Down" | "J" | "Tab" => step(1),
            "Up" | "K" | "Shift+Tab" => step(-1),
            "Home" => step(i64::MIN / 2),
            "End" | "Shift+G" => step(i64::MAX / 2),
            "Return" | "L" | "O" | "Right" => {
                activate(app, window);
                return true;
            }
            "X" | "Delete" => {
                ask_stop(app, window);
                true
            }
            "Escape" | "Q" => {
                commands::close_overlay(app, window);
                return true;
            }
            _ => false,
        }
    };
    if used && is_open(app) {
        show(app, window);
    }
    used
}

fn step(by: i64) -> bool {
    PANEL.with(|p| {
        let p = &mut *p.borrow_mut();
        let last = p.rows.len().saturating_sub(1) as i64;
        p.cursor = (p.cursor as i64).saturating_add(by).clamp(0, last.max(0)) as usize;
    });
    true
}

fn scroll(by: i64) -> bool {
    PANEL.with(|p| {
        let p = &mut *p.borrow_mut();
        // `show` clamps it to the log's length.
        p.back = (p.back as i64).saturating_add(by).max(0) as usize;
    });
    true
}

/// Escape on the output: the list again, or closed when it opened there.
fn back(app: &App, window: &AppWindow) {
    let direct = PANEL.with(|p| p.borrow().direct);
    if direct {
        commands::close_overlay(app, window);
        return;
    }
    PANEL.with(|p| {
        let mut p = p.borrow_mut();
        p.output.clear();
        p.back = 0;
    });
}

/// Enter on a row: a job's output, a helper's chat.
fn activate(app: &Rc<App>, window: &AppWindow) {
    let row = PANEL.with(|p| p.borrow().rows.get(p.borrow().cursor).cloned());
    match row {
        Some(Row::Job(id)) => open_output(app, window, &id),
        Some(Row::Helper(session)) => open_helper(app, window, &session),
        None => {}
    }
}

/// A job row clicked or chosen: its output.
pub fn open_output(app: &App, window: &AppWindow, job_id: &str) {
    if job_id.is_empty() {
        return;
    }
    PANEL.with(|p| {
        let p = &mut *p.borrow_mut();
        if let Some(at) = p.rows.iter().position(|r| *r == Row::Job(job_id.to_owned())) {
            p.cursor = at;
        }
        p.output = job_id.to_owned();
        p.back = 0;
        p.ticks = 0;
    });
    app.engine.borrow_mut().load_job_detail(job_id);
    show(app, window);
}

/// A helper row: its chat opens and the panel closes.
pub fn open_helper(app: &Rc<App>, window: &AppWindow, session: &str) {
    commands::close_overlay(app, window);
    crate::updates_view::open_chat(app, window, session);
}

/// c on a job's output: the chat of the agent that runs it.
fn open_owner(app: &Rc<App>, window: &AppWindow) {
    let output = PANEL.with(|p| p.borrow().output.clone());
    let session = listed_job(&app.engine.borrow(), &output).map(|j| text(&j, "session")).unwrap_or_default();
    if session.is_empty() || app.engine.borrow().roster().find(&session).is_none() {
        return;
    }
    commands::close_overlay(app, window);
    crate::updates_view::open_chat(app, window, &session);
}

/// x: asks before stopping the job on the output, or the row's job or
/// helper.
fn ask_stop(app: &App, window: &AppWindow) {
    let (output, row) = PANEL.with(|p| {
        let p = p.borrow();
        (p.output.clone(), p.rows.get(p.cursor).cloned())
    });
    let row = if output.is_empty() { row } else { Some(Row::Job(output)) };
    let target = {
        let engine = app.engine.borrow();
        match row {
            Some(Row::Job(id)) => listed_job(&engine, &id).filter(stoppable).map(|job| {
                let generation = json::integer(&job, "generation");
                Target::Job { title: job_title(&job), generation: (generation > 0).then_some(generation), id }
            }),
            Some(Row::Helper(session)) => Some(Target::Helper { name: engine.chat_name(&session), session }),
            None => None,
        }
    };
    PANEL.with(|p| p.borrow_mut().confirm = target);
    show(app, window);
}

/// The mouse's Stop on a row.
pub fn ask_stop_row(app: &App, window: &AppWindow, kind: &str, id: &str) {
    let row = if kind == "helper" { Row::Helper(id.to_owned()) } else { Row::Job(id.to_owned()) };
    PANEL.with(|p| {
        let p = &mut *p.borrow_mut();
        if let Some(at) = p.rows.iter().position(|r| *r == row) {
            p.cursor = at;
        }
    });
    ask_stop(app, window);
}

/// The confirm's answer (a key, or its buttons).
pub fn answer(app: &App, window: &AppWindow, stop: bool) {
    if stop {
        confirm_stop(app, window);
    } else {
        PANEL.with(|p| p.borrow_mut().confirm = None);
    }
    show(app, window);
}

fn confirm_stop(app: &App, _window: &AppWindow) {
    let Some(target) = PANEL.with(|p| p.borrow_mut().confirm.take()) else { return };
    match target {
        // The Host fences it to the run shown (`expected_generation`).
        Target::Job { id, generation, .. } => app.engine.borrow_mut().cancel_background_job_run(&id, generation),
        // A helper has no job of its own to cancel: its turn stops (`/stop`).
        Target::Helper { session, name } => {
            app.engine.borrow_mut().stop_session(&session);
            PANEL.with(|p| p.borrow_mut().note = format!("Asked {name} to stop."));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{LINES, log_unavailable, tail, timing};
    use serde_json::json;

    #[test]
    fn the_output_shows_the_newest_lines_and_scrolls_back_within_the_log() {
        let log: String = (1..=30).map(|i| format!("line {i}\n")).collect();
        let (lines, most) = tail(&log, 0);
        assert_eq!((lines.len(), lines.last().map(String::as_str), most), (LINES, Some("line 30"), 30 - LINES));
        let (lines, _) = tail(&log, 5);
        assert_eq!(lines.last().map(String::as_str), Some("line 25"));
        let (lines, _) = tail(&log, 1000);
        assert_eq!(lines.first().map(String::as_str), Some("line 1"), "it stops at the first line");
        assert_eq!(tail("", 0).0, Vec::<String>::new());
    }

    #[test]
    fn a_job_says_how_long_it_ran_and_why_its_log_is_missing() {
        let running = json!({"status": "running", "started_at": 1_000, "heartbeat_at": 61_000});
        assert_eq!(timing(running.as_object().unwrap(), 181_000), "running 3m · ♥ 2m ago");
        let done = json!({"status": "cancelled", "started_at": 1_000, "updated_at": 841_000});
        assert_eq!(timing(done.as_object().unwrap(), 2_000_000), "cancelled after 14m");
        assert_eq!(log_unavailable("forbidden"), "This device may not read process logs.");
        assert_eq!(log_unavailable("odd"), "No log available (odd).");
    }
}
