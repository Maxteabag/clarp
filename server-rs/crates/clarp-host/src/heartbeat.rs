//! The heartbeat at the Host's edge: a turn an idle agent is given so that it looks at what
//! it has standing.
//!
//! The rules are in [`clarp_kernel::heartbeat`]. Here is what is kept (the owner's settings
//! and what is remembered of each agent, whole, with the fleet), what the apps ask (the
//! settings, an agent's switch, an agent's cadence), and the runner that looks once a
//! minute who is due and sends that agent the prompt, as something the Host sends by
//! itself: only to an agent that is idle with nothing waiting, and never to one whose
//! durable goal wakes it.
//!
//! What an agent answers is the conversation's to keep (the actor writes an answer that
//! says no more than that nothing is to be done as one line that says so). What the answer
//! counts as is read here from the turn once it has ended: once for each turn.
//!
//! The routes answer as the Python Host's do (`server/lib/routes/heartbeat.py`): the same
//! statuses, bodies and words.

use std::collections::{BTreeMap, HashMap};
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_actor::conversation::{Versioned, Which};
use clarp_kernel::conversation::Entry;
use clarp_kernel::heartbeat::{self as rules, Counted, Known, Latest, PlanLine, Settings};
use clarp_kernel::python;
use clarp_kernel::turn::Outcome;
use clarp_protocol::requests::SendRequest;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::agent::Unavailable;
use crate::edge::{self, ApiError, App};
use crate::fleet::{Amend, Member};
use crate::{ids, wire};

/// What this desk keeps, declared once (`kept.rs`: the registry walks it at every start).
pub(crate) const KEPT: &[crate::kept::Kind] =
    &[crate::kept::Kind::book(KIND, crate::kept::Key::Plain, fits_book).by_entries(unfit_entries)];

/// The kind of thing the heartbeat's book is, among what is left with the fleet.
const KIND: &str = "heartbeat";
const ALL: &str = "all";
/// How many rows of a conversation are read at a time, and how many times, when the last
/// thing a person said to an agent is looked for.
const ROWS: usize = 200;
const PAGES: usize = 5;
/// How many of an agent's answers to its heartbeats its profile shows.
const HISTORY: usize = 20;

/// What is kept: the owner's settings, what is remembered of each agent by its id, and the
/// heartbeat's turn that was last counted for each.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
struct Book {
    settings: Settings,
    agents: BTreeMap<String, rules::State>,
    counted: BTreeMap<String, String>,
}

/// The heartbeat of this Host. `None` until its book was read where it is kept.
#[derive(Default)]
pub struct Desk {
    book: tokio::sync::Mutex<Option<Book>>,
    /// The entries of its document that are not its book's, put back with every save.
    carried: std::sync::Mutex<crate::kept::Carried>,
}

/// The fields of the heartbeat's book read entry by entry.
const ENTRIES: &[&str] = &["agents", "counted"];

fn fits_book(stored: &[u8]) -> bool {
    crate::kept::fits_carried::<Book, Value>(stored, ENTRIES)
}

fn unfit_entries(stored: &[u8]) -> Vec<String> {
    crate::kept::unfit::<Book, Value>(stored, ENTRIES)
}

impl Desk {
    /// One question to the book. What it changed is kept before this answers.
    async fn ask<T>(&self, app: &App, ask: impl FnOnce(&mut Book) -> T) -> Result<T, Unavailable> {
        let mut held = self.book.lock().await;
        if held.is_none() {
            // Through the loader: a document that is not the book's is set aside, an entry that
            // is not is carried through (`kept.rs`); nobody is woken on a guess.
            let (book, carried) = app
                .fleet
                .read_book::<Book, Value>(KIND, ALL, ENTRIES)
                .await?;
            if let Ok(mut kept) = self.carried.lock() {
                *kept = carried;
            }
            *held = Some(book);
        }
        let Some(book) = held.as_mut() else {
            return Err(Unavailable::new(
                "the heartbeat's book could not be read".to_string(),
            ));
        };
        let mut next = book.clone();
        let answer = ask(&mut next);
        if next != *book {
            let carried = self.carried.lock().map(|c| c.clone()).unwrap_or_default();
            let kept = crate::kept::saved(&next, &carried);
            app.fleet.keep(KIND, ALL.to_string(), Some(kept)).await?;
            *book = next;
            app.fleet.written_over(KIND, ALL, &self.carried, &*book);
        }
        Ok(answer)
    }
}

/// What the heartbeat sees of an agent at a moment.
struct Seen {
    latest: Latest,
    /// A turn of it runs, or one waits.
    busy: bool,
    /// Its queue is held: what the Host sends by itself waits until its user lets it go.
    held: bool,
    /// When a person last said something to it, or it answered a person, in milliseconds.
    /// Nothing: not within what was looked through.
    last_person_ms: i64,
    team_activity_ms: i64,
    promotion_ms: i64,
    /// The heartbeat's turn that ended last, by a name no other turn has, and what its
    /// answers count as.
    turn: Option<(String, Counted)>,
    /// A durable goal of its own wakes it: the heartbeat leaves it alone.
    woken_by_a_goal: bool,
    /// The plan it has standing: its title, and what is in it.
    plan: Option<(String, Vec<PlanLine>)>,
    /// Its answers to heartbeats, the newest first.
    history: Vec<Value>,
}

/// What a heartbeat's turn counts as: what its last answer says. Nothing found, when that
/// is the line for it; activity, when it says something else.
///
/// What an agent remarks on its way to its answer is not what it found. The Python Host
/// counts every message of the turn, and one remark ("I will look at the plan first")
/// makes a turn that found nothing count as activity: an agent whose tool narrates is then
/// never asked less often and never goes dormant (reference defect R28). Measured on the
/// owner's Host over seven days, 87 of 122 heartbeat turns of Codex agents and 230 of 280
/// of Muse agents counted as activity, against 4 of 31 of Claude agents.
fn counted(rows: &[Versioned], turn: u64) -> Counted {
    let of_the_turn = rows.iter().filter(|row| row.turn_number == Some(turn));
    let mut said = of_the_turn.filter_map(|row| match &row.entry {
        // A message that only calls tools says nothing: it is no answer.
        Entry::Reply(reply) if !python::strip(&reply.text).is_empty() => Some(&reply.text),
        _ => None,
    });
    match said.next_back() {
        Some(last) if last == rules::NOTHING_TO_DO => Counted::Nothing,
        Some(last) if !rules::is_neutral_status(last) => Counted::Activity,
        _ => Counted::Neither,
    }
}

impl Seen {
    fn wake_signal_ms(&self) -> i64 {
        self.last_person_ms
            .max(self.team_activity_ms)
            .max(self.promotion_ms)
    }
}

/// Whether a row is of a conversation with a person: what one said, or an answer to it.
fn of_a_person(row: &Versioned) -> bool {
    matches!(row.entry, Entry::Message(_) | Entry::Reply(_)) && row.entry.origin_name() == "user"
}

async fn seen(app: &App, member: &Member) -> Seen {
    let view = member.agent.view();
    let (status, record) = (&view.status, &member.record);
    let kind = wire::state_name(&wire::state_kind(status));
    let since = match (&status.live, &status.last) {
        (Some(live), _) => live.started_at_ms,
        (None, Some(last)) => last.ended_at_ms,
        (None, None) => record.created_at_ms,
    };
    // A turn its user stopped is not taken up against its user's word.
    let last = status.last.as_ref().filter(|_| status.live.is_none());
    let stopped = matches!(
        last.map(|last| &last.outcome),
        Some(Outcome::Stopped { .. })
    );
    let detail = if stopped {
        json!({ "source": "user_stop" })
    } else {
        Value::Null
    };
    let latest = Latest {
        kind,
        ts_ms: since as f64,
        detail,
    };
    let goals = &view.books.goals;
    let woken_by_a_goal = goals.plans().any(|plan| {
        let standing = matches!(plan.status.as_str(), "active" | "paused" | "blocked");
        plan.recovery_enabled != 0 && standing
    });
    let active = goals.plans().filter(|plan| plan.status == "active");
    let plan = active.max_by_key(|plan| (plan.updated_at, plan.order));
    let plan = plan.map(|plan| {
        let line = |item: &clarp_kernel::goals::Item| PlanLine {
            status: match item.status.as_str() {
                "" => "pending".to_string(),
                status => status.to_string(),
            },
            title: item.title.clone(),
        };
        let parts_of = |parent: Option<&str>| {
            let items = plan.items.iter();
            let mut parts: Vec<_> = items
                .filter(|item| item.parent_id.as_deref() == parent)
                .collect();
            parts.sort_by_key(|item| item.position);
            parts
        };
        let mut lines = Vec::new();
        for item in parts_of(None) {
            lines.push(line(item));
            let parts = parts_of(Some(item.item_id.as_str()));
            lines.extend(parts.into_iter().map(line));
        }
        (plan.title.clone(), lines)
    });
    // The newest rows: the answers of the last heartbeat, the answers its profile shows,
    // and, as far back as is looked, the last thing a person said.
    let newest = member.agent.tail(ROWS, Which::All).await.ok();
    let rows = newest.as_ref().map_or(&[][..], |page| &page.entries[..]);
    let turn = last
        .filter(|last| last.origin.name() == "heartbeat")
        .map(|last| {
            let name = format!("{}:{}", last.id.0, last.ended_at_ms);
            (name, counted(rows, last.id.0))
        });
    let answers = rows.iter().rev().filter_map(|row| match &row.entry {
        Entry::Reply(reply) if reply.origin.name() == "heartbeat" => Some(json!({
            "id": reply.id,
            "text": reply.text,
            "timestamp": wire::iso(row.ts_ms),
            "updated_at": row.ts_ms,
        })),
        _ => None,
    });
    let history = answers.take(HISTORY).collect();
    let mut last_person_ms = rows
        .iter()
        .rev()
        .find(|row| of_a_person(row))
        .map(|row| row.ts_ms);
    let mut oldest = rows.first().map(|row| row.entry.id().to_string());
    let mut more = newest.as_ref().is_some_and(|page| page.has_more);
    for _ in 1..PAGES {
        let (None, true, Some(before)) = (last_person_ms, more, oldest.take()) else {
            break;
        };
        let Ok(Some(page)) = member.agent.before(before, ROWS, Which::All).await else {
            break;
        };
        let rows = page.entries.iter();
        last_person_ms = rows.rev().find(|row| of_a_person(row)).map(|row| row.ts_ms);
        oldest = page.entries.first().map(|row| row.entry.id().to_string());
        more = page.has_more;
    }
    Seen {
        latest,
        busy: status.live.is_some() || !status.waiting.is_empty(),
        held: status.paused,
        last_person_ms: last_person_ms.unwrap_or(0),
        team_activity_ms: crate::teams::runtime::latest_activity(app, &record.agent_id),
        promotion_ms: app.teams.latest_promotion(),
        turn,
        woken_by_a_goal,
        plan,
        history,
    }
}

/// Seconds, of milliseconds.
fn seconds(ms: i64) -> f64 {
    ms as f64 / 1000.0
}

/// What is remembered of an agent, brought up to what was seen of it: the heartbeat's turn
/// that ended is counted, once, and a person who spoke to it since wakes it.
fn caught_up<'a>(book: &'a mut Book, agent_id: &str, seen: &Seen) -> &'a mut rules::State {
    let Book {
        settings,
        agents,
        counted,
    } = book;
    let state = agents.entry(agent_id.to_string()).or_default();
    // A run of interruptions begins with its first, and is not begun anew by a heartbeat
    // whose own turn is interrupted.
    state.saw(&seen.latest);
    if let Some((turn, what)) = &seen.turn
        && counted.get(agent_id) != Some(turn)
    {
        match what {
            Counted::Activity => state.was_active(),
            Counted::Nothing => state.found_nothing(settings, seen.latest.interrupted()),
            Counted::Neither => {}
        }
        counted.insert(agent_id.to_string(), turn.clone());
    }
    state.woken_by(seen.wake_signal_ms());
    state
}

/// The prompt for an agent whose heartbeat is due now, when it is; and it is remembered as
/// started. Why not, otherwise, is the kernel's to say.
fn due(book: &mut Book, agent_id: &str, seen: &Seen, now_ms: i64) -> Option<String> {
    let policy = book.settings.clone();
    let state = caught_up(book, agent_id, seen);
    let now = seconds(now_ms);
    // Somebody spoke with it a moment ago: it is left alone for as long as its interval is.
    let age = now - seconds(seen.last_person_ms);
    let quiet = policy.interval_sec as f64;
    let recent_activity = if seen.last_person_ms != 0 && age < quiet {
        format!("recent-activity:{}s", (age.max(0.0)) as i64)
    } else {
        String::new()
    };
    let known = Known {
        latest: seen.latest.clone(),
        busy: seen.busy,
        active: false,
        compacting: false,
        recent_activity,
    };
    let why_not = rules::skip_reason(&known, state, &policy, now);
    if why_not == "dormant" {
        state.dormant = true;
    }
    if !why_not.is_empty() {
        return None;
    }
    state.started(now, seen.wake_signal_ms());
    let interrupted = seen.latest.interrupted() && !seen.latest.taken_down_by_a_restart();
    let plan = seen.plan.as_ref();
    let plan = plan.map(|(title, lines)| (title.as_str(), lines.as_slice()));
    Some(rules::prompt_text(interrupted, plan))
}

/// The runner's turn: every agent that has the heartbeat on is looked at, and the ones
/// that are due are sent the prompt.
async fn tick(app: &App) {
    if app.config.heartbeats_disabled {
        return;
    }
    let directory = app.fleet.directory();
    for member in &directory.members {
        let record = &member.record;
        let here = record.archived_at_ms.is_none() && record.deleted_at_ms.is_none();
        if !record.heartbeat_enabled || !here || member.agent.view().status.moved.is_some() {
            continue;
        }
        let seen = seen(app, member).await;
        if seen.woken_by_a_goal {
            continue;
        }
        let now_ms = app.clock.now_ms();
        let desk = app.heartbeat.clone();
        let asked = desk.ask(app, |book| due(book, &record.agent_id, &seen, now_ms));
        let Ok(Some(prompt)) = asked.await else {
            continue;
        };
        // Only an agent that is idle with nothing held is sent it: the heartbeat was due
        // and is counted as started either way, so that it is not tried again at once.
        if seen.held {
            continue;
        }
        let request = SendRequest {
            session: record.session.clone(),
            text: prompt,
            client_msg_id: Some(format!("heartbeat-{}", ids::hex(8))),
            synthesize_audio: Some(false),
            hands_free: Some(false),
            force_session: Some(true),
            queue_if_busy: Some(true),
            origin: Some("heartbeat".to_string()),
            ..SendRequest::default()
        };
        if let Err(refused) = edge::send_own(app, request).await {
            eprintln!(
                "clarp-host: a heartbeat for {} was not sent: {}",
                record.session,
                refused.message()
            );
        }
    }
}

/// Starts the runner: it looks who is due every so often (once a minute, as a Host comes),
/// the first time that long from now. A Host that has just come up wakes nobody.
pub fn run(app: &App) {
    let app = app.clone();
    let every = Duration::from_millis(app.config.heartbeats_checked_every_ms.max(50));
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(every).await;
            tick(&app).await;
        }
    });
}

// ---- the routes ----------------------------------------------------------------------------

fn refuse(status: StatusCode, why: &str) -> Response {
    ApiError::new(status, why).into_response()
}

/// What a request says: an object, or nothing at all, which is an empty one.
fn said(bytes: &Bytes) -> Option<serde_json::Map<String, Value>> {
    let said = if bytes.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(bytes).unwrap_or(Value::Null)
    };
    match said {
        Value::Object(fields) => Some(fields),
        _ => None,
    }
}

fn shown(settings: &Settings) -> Response {
    let shown = json!({
        "ok": true,
        "settings": settings.shown(),
        "heartbeat_prompt": rules::PROMPT,
    });
    Json(shown).into_response()
}

/// `GET /heartbeat/settings`: the owner's settings for every agent's heartbeat.
async fn settings(State(app): State<App>) -> Response {
    let desk = app.heartbeat.clone();
    match desk.ask(&app, |book| book.settings.clone()).await {
        Ok(settings) => shown(&settings),
        Err(unavailable) => ApiError::from(unavailable).into_response(),
    }
}

/// `POST /heartbeat/settings`: the settings are changed, all of what is named or nothing.
async fn change_settings(State(app): State<App>, bytes: Bytes) -> Response {
    let Some(said) = said(&bytes) else {
        return refuse(StatusCode::BAD_REQUEST, "bad json");
    };
    let desk = app.heartbeat.clone();
    let changed = desk.ask(&app, |book| {
        let changed = book.settings.changed(&Value::Object(said))?;
        book.settings = changed.clone();
        Ok::<Settings, String>(changed)
    });
    match changed.await {
        Ok(Ok(settings)) => shown(&settings),
        Ok(Err(why)) => refuse(StatusCode::BAD_REQUEST, &why),
        Err(unavailable) => ApiError::from(unavailable).into_response(),
    }
}

/// `POST /agent-heartbeat` with `{session, heartbeat_enabled}`: an agent's heartbeat is
/// turned on or off. Either way the agent is asked again at the owner's interval.
async fn switch(State(app): State<App>, bytes: Bytes) -> Response {
    let Some(said) = said(&bytes) else {
        return refuse(StatusCode::BAD_REQUEST, "bad json");
    };
    let session = match edge::body_text(&said, "session") {
        Ok(session) => session,
        Err(why) => return why.into_response(),
    };
    if session.is_empty() {
        return refuse(StatusCode::BAD_REQUEST, "session required");
    }
    let Some(enabled) = said.get("heartbeat_enabled") else {
        return refuse(StatusCode::BAD_REQUEST, "heartbeat_enabled required");
    };
    let Some(enabled) = enabled.as_bool() else {
        return refuse(StatusCode::BAD_REQUEST, "heartbeat_enabled must be boolean");
    };
    let amended = app
        .fleet
        .amend(session.to_string(), Amend::Heartbeat(enabled));
    let record = match amended.await {
        Ok(Ok(record)) => record,
        Ok(Err(_)) => return refuse(StatusCode::NOT_FOUND, "no such agent"),
        Err(unavailable) => return ApiError::from(unavailable).into_response(),
    };
    let desk = app.heartbeat.clone();
    let active = desk.ask(&app, |book| {
        let state = book.agents.entry(record.agent_id.clone()).or_default();
        state.was_active();
    });
    if let Err(unavailable) = active.await {
        return ApiError::from(unavailable).into_response();
    }
    let answer = json!({
        "ok": true,
        "session": session,
        "heartbeat_enabled": record.heartbeat_enabled,
    });
    Json(answer).into_response()
}

/// `GET /agent-heartbeat/status?session=`: an agent's cadence as it stands, and its last
/// answers to heartbeats.
async fn status(State(app): State<App>, Query(asked): Query<HashMap<String, String>>) -> Response {
    let session = asked.get("session").map_or("", |said| python::strip(said));
    let directory = app.fleet.directory();
    let Some(member) = directory.named(session) else {
        return refuse(StatusCode::NOT_FOUND, "no such agent");
    };
    let seen = seen(&app, member).await;
    let now = seconds(app.clock.now_ms());
    let enabled = member.record.heartbeat_enabled;
    let agent_id = member.record.agent_id.clone();
    let desk = app.heartbeat.clone();
    let schedule = desk.ask(&app, |book| {
        let policy = book.settings.clone();
        let state = caught_up(book, &agent_id, &seen);
        let quiet_until = match seen.last_person_ms {
            0 => 0.0,
            last => seconds(last) + policy.interval_sec as f64,
        };
        rules::schedule(
            enabled,
            (&seen.latest, state),
            &policy,
            (now, quiet_until, false),
        )
    });
    match schedule.await {
        Ok(schedule) => {
            let answer = json!({
                "ok": true,
                "session": session,
                "schedule": schedule,
                "history": seen.history,
            });
            Json(answer).into_response()
        }
        Err(unavailable) => ApiError::from(unavailable).into_response(),
    }
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/heartbeat/settings", get(settings).post(change_settings))
        .route("/agent-heartbeat", post(switch))
        .route("/agent-heartbeat/status", get(status))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod due_work_tests {
    use super::*;

    #[test]
    fn teams_runtime_new_team_activity_wakes_a_dormant_heartbeat_only_once() {
        let id = "agent";
        let mut book = Book::default();
        book.agents.entry(id.into()).or_default().dormant = true;
        let mut seen = Seen {
            latest: Latest {
                kind: "idle".into(),
                ts_ms: 0.0,
                detail: Value::Null,
            },
            busy: false,
            held: false,
            last_person_ms: 0,
            team_activity_ms: 5_000,
            promotion_ms: 0,
            turn: None,
            woken_by_a_goal: false,
            plan: None,
            history: Vec::new(),
        };
        let state = caught_up(&mut book, id, &seen);
        assert!(!state.dormant);
        assert_eq!(state.last_wake_signal_ms, 5_000);
        state.dormant = true;
        assert!(caught_up(&mut book, id, &seen).dormant);
        seen.team_activity_ms = 6_000;
        let state = caught_up(&mut book, id, &seen);
        assert!(!state.dormant);
        state.dormant = true;
        seen.promotion_ms = 7_000;
        let state = caught_up(&mut book, id, &seen);
        assert!(!state.dormant);
        assert_eq!(state.last_wake_signal_ms, 7_000);
    }

    #[tokio::test]
    async fn the_cached_book_matches_what_the_store_accepted() {
        let place = crate::unkept::at_the_port::place("heartbeat-kept");
        let app = place.app().await;
        let desk = &app.heartbeat;
        let read = |book: &mut Book| book.settings.interval_sec;
        desk.ask(&app, |book| book.settings.interval_sec = 600)
            .await
            .unwrap();
        place.refuse_writes(Some(KIND));
        let refused = desk
            .ask(&app, |book| book.settings.interval_sec = 900)
            .await;
        assert!(refused.unwrap_err().0.contains("refused here"));
        assert_eq!(desk.ask(&app, read).await.unwrap(), 600);
        assert_eq!(Desk::default().ask(&app, read).await.unwrap(), 600);
        place.refuse_writes(None);
        desk.ask(&app, |book| book.settings.interval_sec = 900)
            .await
            .unwrap();
        assert_eq!(desk.ask(&app, read).await.unwrap(), 900);
        assert_eq!(Desk::default().ask(&app, read).await.unwrap(), 900);
    }
}
