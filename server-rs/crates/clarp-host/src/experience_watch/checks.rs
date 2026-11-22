//! Observe one window of Host state. Refused reads leave that finding open.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use clarp_actor::{QueueItemStatus, Waiting};
use clarp_kernel::experience::{Window, is_user_wait};
use clarp_store_sqlite::client_events::CrashCandidate;
use futures_util::{StreamExt as _, stream};
use serde_json::Value;

use crate::edge::App;
use crate::fleet::Directory;

use super::{Relaunches, book::Seen, release, settings::Settings};

const RECENT_MS: i64 = 24 * 60 * 60 * 1_000;

pub(crate) struct AgentFacts {
    pub queue: Vec<Waiting>,
    pub parked: Option<i64>,
    pub paused: bool,
    pub window: Window,
}

fn agent(
    seen: &mut Seen,
    settings: &Settings,
    subject: &str,
    now: i64,
    facts: &AgentFacts,
    refusals: usize,
) {
    let day_ago = now.saturating_sub(RECENT_MS);
    let mut current_waits = 0;
    let mut longest = 0;
    let (mut queued, mut claimed, mut parked, mut oldest) = (0, 0, 0, 0);
    let mut stuck = false;
    for row in facts.queue.iter().filter(|row| {
        row.status != QueueItemStatus::Started
            && is_user_wait(&row.origin, &row.client_id, row.sender_agent_id.is_some())
    }) {
        let waited = now.saturating_sub(row.accepted_at_ms);
        if waited > settings.wait_ms {
            current_waits += 1;
            longest = longest.max(waited);
        }
        if row.accepted_at_ms >= day_ago {
            match row.status {
                QueueItemStatus::Queued => queued += 1,
                QueueItemStatus::Claimed => claimed += 1,
                QueueItemStatus::Started => {}
            }
            oldest = oldest.max(waited);
            stuck |= waited > settings.stuck_ms;
        }
    }
    if let Some(at) = facts.parked.filter(|at| *at >= day_ago) {
        parked = 1;
        oldest = oldest.max(now.saturating_sub(at));
    }
    if stuck || (facts.paused && queued + parked > 0) {
        seen.add("queue", subject, format!(
            "Paused: {}. User rows: {queued} queued, {claimed} claimed, {parked} parked. Oldest {} s.",
            facts.paused, oldest / 1_000,
        ));
    }

    let mut steered = 0;
    let mut waited = 0;
    for read in &facts.window.reads {
        if read.waited_ms > settings.wait_ms {
            if read.steered {
                steered += 1;
            } else {
                waited += 1;
            }
            longest = longest.max(read.waited_ms);
        }
    }
    let late = waited + steered + current_waits;
    if late > 0 && late >= settings.wait_count {
        seen.add("wait", subject, format!(
            "{steered} steered, {waited} waited for a turn, {current_waits} still waiting. Longest {} s.",
            longest / 1_000,
        ));
    }
    sends(seen, settings, subject, facts.window.failed, refusals);
}

fn sends(seen: &mut Seen, settings: &Settings, subject: &str, failed: usize, refused: usize) {
    let total = failed.saturating_add(refused);
    if total > 0 && total >= settings.failed_sends {
        seen.add(
            "sends",
            subject,
            format!("{failed} failed or cancelled after admission, {refused} refused HTTP sends."),
        );
    }
}

fn cards(seen: &mut Seen, settings: &Settings, directory: &Directory, now: i64) {
    let mut count = 0;
    let mut oldest = 0;
    let mut agents: BTreeSet<String> = BTreeSet::new();
    for member in directory.members.iter().chain(&directory.janitors) {
        let view = member.agent.view();
        if view.broken.is_some() {
            seen.unavailable_kinds.insert("cards");
        }
        for card in view.books.artifacts.artifacts() {
            let age = now.saturating_sub(card.created_at);
            if age > settings.card_age_ms
                && card.deleted_at.is_none()
                && card.archived_at.is_none()
                && card.decision.as_ref().is_some_and(|decision| {
                    decision.status == "pending" && decision.expires_at.is_none_or(|at| at > now)
                })
            {
                count += 1;
                oldest = oldest.max(age);
                agents.insert(super::book::agent_label(
                    &member.record.persona,
                    &member.record.session,
                ));
            }
        }
    }
    if count > 0 {
        let named = agents.into_iter().collect::<Vec<_>>().join(", ");
        seen.add(
            "cards",
            "open",
            format!(
                "{count} open decision cards. Oldest {} h. Agents: {named}.",
                oldest / 3_600_000,
            ),
        );
    }
}

fn crash_count(row: &CrashCandidate) -> u64 {
    let event = row.event.to_ascii_lowercase();
    if event == "metrickit_report" {
        return row
            .report
            .as_deref()
            .and_then(|report| serde_json::from_str::<Value>(report).ok())
            .and_then(|report| report["crashes"].as_u64())
            .unwrap_or(0);
    }
    // Severity alone is not a crash: the apps also log error and fault
    // levels for background sync, reconnects, hangs, and other diagnostics.
    let kind = event.rsplit(['.', ':']).next().unwrap_or_default();
    u64::from(matches!(
        kind,
        "crash"
            | "app_crash"
            | "app-crash"
            | "fatal"
            | "fatal_error"
            | "fatal-error"
            | "uncaught_exception"
            | "uncaught-exception"
    ))
}

pub(super) fn platform(value: &str) -> bool {
    matches!(value, "ios" | "android" | "desktop" | "web")
}

#[derive(Default)]
struct Crashes {
    count: u64,
    build: String,
    contract: Option<i64>,
}

fn app_crashes(
    seen: &mut Seen,
    settings: &Settings,
    rows: Vec<CrashCandidate>,
    launches: &BTreeMap<String, Relaunches>,
) {
    let mut crashes: BTreeMap<(String, String), Crashes> = BTreeMap::new();
    for row in rows {
        let count = crash_count(&row);
        if count == 0 {
            continue;
        }
        let launch = launches.get(&row.device);
        let client = if platform(&row.client) {
            row.client.as_str()
        } else if !row.client.is_empty() {
            "unknown"
        } else if row.event == "metrickit_report" {
            "ios"
        } else {
            launch
                .filter(|launch| platform(&launch.platform))
                .map_or("unknown", |launch| launch.platform.as_str())
        };
        let crash = crashes.entry((row.device, client.to_owned())).or_default();
        crash.count = crash.count.saturating_add(count);
        if !row.build.is_empty() {
            crash.build = row.build;
        }
        if row.contract.is_some() {
            crash.contract = row.contract;
        }
    }
    let threshold = u64::try_from(settings.crash_events).unwrap_or(u64::MAX);
    for ((device, client), crash) in crashes {
        if crash.count < threshold {
            continue;
        }
        let launch = launches.get(&device);
        let matching_launch = launch.filter(|launch| launch.platform == client);
        let build = if !crash.build.is_empty() {
            &crash.build
        } else {
            matching_launch.map_or("unknown", |launch| launch.build.as_str())
        };
        let contract = crash
            .contract
            .or_else(|| matching_launch.and_then(|launch| launch.contract))
            .map_or_else(|| "unknown".to_owned(), |n| n.to_string());
        let foreground = launch.map_or(0, |launch| launch.foreground_sessions);
        let legacy = launch.map_or(0, |launch| launch.legacy_requests);
        seen.add(
            &format!("app_crash_{client}"),
            &device,
            format!(
                "{} crash events. {foreground} observed foreground sessions. {legacy} unmarked requests to /devices or /server-info. Build {build}. Contract {contract}.",
                crash.count,
            ),
        );
    }
}

pub(super) async fn collect(app: &App, settings: &Settings, since: i64, now: i64) -> Seen {
    let directory = app.fleet.directory();
    let mut seen = Seen::default();
    let refusals = app.experience.refusals(since, now);

    let members: Vec<_> = directory
        .members
        .iter()
        .chain(&directory.janitors)
        .cloned()
        .collect();
    let mut agents = stream::iter(members)
        .map(|member| async move {
            let facts =
                tokio::time::timeout(Duration::from_secs(2), member.agent.experience(since, now))
                    .await;
            (member, facts)
        })
        .buffer_unordered(8);
    while let Some((member, facts)) = agents.next().await {
        let id = member.record.agent_id.as_str();
        let refused = refusals.get(id).copied().unwrap_or(0);
        if let Ok(Ok(facts)) = facts {
            agent(&mut seen, settings, id, now, &facts, refused);
        } else {
            eprintln!("clarp-host: experience-watch could not read sends/queue of {id}");
            for kind in ["wait", "queue", "sends"] {
                seen.missing(kind, id);
            }
            sends(&mut seen, settings, id, 0, refused);
        }
        let view = member.agent.view();
        if view.broken.is_some() {
            seen.unavailable_kinds.insert("release");
        }
        for job in &view.jobs {
            release::collect(&mut seen, settings, job, now);
        }
    }

    cards(&mut seen, settings, &directory, now);
    match app.fleet.computer_jobs().await {
        Ok(jobs) => {
            for job in &jobs {
                release::collect(&mut seen, settings, job, now);
            }
        }
        Err(why) => {
            seen.unavailable_kinds.insert("release");
            eprintln!(
                "clarp-host: experience-watch could not read computer jobs: {}",
                why.0
            );
        }
    }

    match crate::client_events::crash_candidates(app, since, now).await {
        Ok(rows) => app_crashes(
            &mut seen,
            settings,
            rows,
            &app.experience.relaunches(since, now),
        ),
        Err(why) => {
            seen.unavailable_kinds.extend([
                "app_crash_ios",
                "app_crash_android",
                "app_crash_desktop",
                "app_crash_web",
                "app_crash_unknown",
            ]);
            eprintln!("clarp-host: experience-watch could not read crash signals: {why}");
        }
    }

    match crate::managed_skills::status(app).await {
        Ok(skills) => {
            for backend in ["claude", "codex", "agy"] {
                let broken = skills
                    .iter()
                    .filter(|skill| {
                        skill["enabled"] == true
                            && skill["links"]
                                .get(backend)
                                .is_some_and(|link| link != "healthy")
                    })
                    .filter_map(|skill| skill["id"].as_str())
                    .collect::<Vec<_>>();
                if !broken.is_empty() {
                    seen.add(
                        "skills",
                        backend,
                        format!("Unhealthy skill links: {}.", broken.join(", ")),
                    );
                }
            }
        }
        Err(why) => {
            seen.unavailable_kinds.insert("skills");
            eprintln!(
                "clarp-host: experience-watch could not read managed skills: {}",
                why.message()
            );
        }
    }
    seen
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use crate::experience_watch::book::{Book, Notice};
    use axum::{
        body::Body,
        http::{Method, Request, StatusCode, header},
    };
    use clarp_kernel::experience::Read;
    use clarp_kernel::turn::{Admission, Origin, TurnId};
    use tower::ServiceExt as _;

    fn release_steps(settings: &Settings, zone: &jiff::tz::TimeZone, now: i64) {
        let job: clarp_kernel::jobs::Job = serde_json::from_value(serde_json::json!({
            "id": "release-fixture",
            "kind": "test",
            "title": "Release candidate",
            "status": "running",
            "generation": 1,
            "revision": 1,
            "started_at_ms": now - 25 * 60_000,
            "updated_at_ms": now,
            "heartbeat_timeout_ms": 600_000,
            "heartbeat_source": "registration",
        }))
        .unwrap();
        let command = |line: &str| {
            release::step(
                &line
                    .split_whitespace()
                    .map(str::to_owned)
                    .collect::<Vec<_>>(),
            )
            .unwrap()
        };
        let check = command("cargo check");
        let mut seen = Seen::default();
        for args in [
            "ssh build-host ./release_build.sh",
            "bash ./release_build.sh",
            "cargo build",
            "./tools/build release",
            "xcodebuild build-for-testing -scheme test",
            "./tools/install-host",
        ] {
            release::observe(
                &mut seen,
                settings,
                &job,
                &[(command(args), 14 * 60_000), (check, 14 * 60_000)],
                now,
                zone,
            );
        }
        release::observe(&mut seen, settings, &job, &[(check, 60_000)], now, zone);
        assert!(seen.found.is_empty());

        for kind in ["release", "build", "install"] {
            let mut other = job.clone();
            other.kind = kind.into();
            release::observe(
                &mut seen,
                settings,
                &other,
                &[(check, 14 * 60_000)],
                now,
                zone,
            );
        }
        assert!(seen.found.is_empty());

        release::observe(
            &mut seen,
            settings,
            &job,
            &[(check, 14 * 60_000)],
            now,
            zone,
        );
        assert_eq!(
            seen.found[&("release".into(), job.id)],
            "Registered kind test. Active step cargo check has held the release for 14 min, since 2026-11-22 16:09 +01:00.",
        );
    }

    fn wait_provenance(settings: &Settings, now: i64) {
        use clarp_kernel::agent::Command;
        use clarp_kernel::conversation::{Message, Sender};
        use clarp_kernel::journal::{Incoming, Recorder};
        use clarp_kernel::turn::RunOutcome;
        use clarp_ports::log::LogEngine as _;

        let cases = [
            ("user", Origin::User, false, true),
            (
                "oracle",
                Origin::Trigger {
                    name: "oracle".into(),
                },
                false,
                true,
            ),
            (
                "agent",
                Origin::Agent {
                    agent_id: "sender".into(),
                },
                true,
                false,
            ),
            (
                "watcher",
                Origin::Agent {
                    agent_id: String::new(),
                },
                true,
                false,
            ),
            ("html-form-fixture", Origin::User, false, false),
            ("mislabeled", Origin::User, true, false),
            (
                "schedule",
                Origin::Trigger {
                    name: "schedule".into(),
                },
                false,
                false,
            ),
        ];
        for (id, origin, has_sender, counts) in cases {
            let engine = clarp_store_mem::MemEngine::new();
            let open = || {
                engine
                    .open(
                        std::path::Path::new("watch-wait"),
                        clarp_codec::standard_set("none").unwrap(),
                    )
                    .unwrap()
            };
            let mut recorder = Recorder::open(open()).unwrap();
            let first = recorder
                .accept(
                    serde_json::to_vec(&Message::default()).unwrap(),
                    Origin::User,
                    now - 180_000,
                )
                .unwrap()
                .turn
                .unwrap();
            let message = Message {
                id: format!("u-{id}"),
                text: "Fixture".into(),
                origin: origin.clone(),
                sender: has_sender.then(|| Sender {
                    agent_id: "sender".into(),
                    name: "Sender".into(),
                    session: "sender".into(),
                }),
                ..Message::default()
            };
            let at = now - 120_000;
            let second = recorder
                .accept_message(
                    Incoming {
                        key: Some(message.id.clone()),
                        body: serde_json::to_vec(&message).unwrap(),
                        origin: origin.clone(),
                        admission: Admission::Later,
                        coordination: None,
                        user_word: false,
                    },
                    at,
                )
                .unwrap()
                .turn
                .unwrap();

            let facts = AgentFacts {
                queue: vec![Waiting {
                    turn: second,
                    message_id: message.id.clone(),
                    client_id: message.client_id().into(),
                    told_id: message.told_id().into(),
                    text: message.text,
                    speak: false,
                    accepted_at_ms: at,
                    admission: Admission::Later,
                    status: QueueItemStatus::Queued,
                    origin,
                    sender_agent_id: message.sender.map(|sender| sender.agent_id),
                }],
                parked: None,
                paused: true,
                window: Window::default(),
            };
            let mut seen = Seen::default();
            agent(&mut seen, settings, "receiver", now, &facts, 0);
            for kind in ["wait", "queue"] {
                assert_eq!(
                    seen.found.contains_key(&(kind.into(), "receiver".into())),
                    counts,
                    "{id} for {kind} in mailbox",
                );
            }

            let mut batch = recorder.batch();
            batch
                .command(
                    Command::TurnEnded {
                        turn: first,
                        outcome: RunOutcome::Completed,
                    },
                    now - 30_000,
                )
                .unwrap();
            batch
                .command(Command::TurnStarted { turn: second }, now)
                .unwrap();
            batch.commit().unwrap();
            let live = recorder.experience_window(at, now).unwrap();
            let replayed = Recorder::open(open())
                .unwrap()
                .experience_window(at, now)
                .unwrap();
            assert_eq!(live.reads.len(), usize::from(counts), "{id} live journal");
            assert_eq!(live.reads, replayed.reads, "{id} after replay");
        }
    }

    #[tokio::test]
    async fn experience_watch_findings_open_clear_and_summarize_once() {
        let place = crate::unkept::at_the_port::place("experience-watch-pass");
        let mut app = place.app().await;
        std::sync::Arc::make_mut(&mut app.config).auth_token = "fixture-admin".into();
        let zone = jiff::tz::TimeZone::get("Europe/Oslo").unwrap();
        let now = "2026-11-22T15:23:00Z"
            .parse::<jiff::Timestamp>()
            .unwrap()
            .as_millisecond();
        place.clock.advance_ms(now - app.clock.now_ms());
        let scope = crate::pairing::Scope::Full;
        let issued = app
            .fleet
            .issue("iPhone".into(), scope, 60)
            .await
            .unwrap()
            .unwrap();
        let phone = app
            .fleet
            .exchange(issued.code, String::new())
            .await
            .unwrap()
            .unwrap();
        let watched = app
            .fleet
            .create(clarp_protocol::requests::CreateAgentRequest {
                name: "watch proof (test)".into(),
                session: Some("watch-proof-test".into()),
                backend: Some("scripted".into()),
                ..Default::default()
            })
            .await
            .unwrap()
            .unwrap();
        let agent_id = &watched.agent_id;
        let agent_label = "watch proof (test) (watch-proof-test)";
        let directory = app.fleet.directory();
        let config: toml::Value = "enabled = true\nsummary_hour = 17\nfront_desk = 'desk'\n[owners]\napp_crash = 'ios-owner'\nqueue = 'desk'\n[thresholds]\ncrash_events = 3\nwindow_minutes = 3\n".parse().unwrap();
        let settings = Settings::from_section(Some(&config));
        release_steps(&settings, &zone, now);
        wait_provenance(&settings, now);
        let since = settings.window_start(now, now);
        assert_eq!(since, now - 300_000);
        assert_eq!(settings.window_start(now, now - 360_000), now - 360_000);
        let router = crate::edge::router(app.clone());
        let request = |method: Method, path, token: &str, client, state| {
            let body = if method == Method::POST {
                format!(r#"{{"token":"{}"}}"#, "a".repeat(64))
            } else {
                String::new()
            };
            let mut request = Request::builder()
                .method(method)
                .uri(path)
                .header(header::AUTHORIZATION, format!("Bearer {token}"))
                .header(header::CONTENT_TYPE, "application/json")
                .header(header::CONTENT_LENGTH, body.len())
                .header("x-clarp-client", client);
            if let Some(state) = state {
                request = request.header("x-clarp-app-state", state);
            }
            let request = request.body(Body::from(body)).unwrap();
            let router = router.clone();
            async move { router.oneshot(request).await.unwrap().status() }
        };
        let ios = "ios/200 contract=130";
        // Background requests do not add a launch. A background marker on
        // another API route can rearm the device for a later foreground.
        for background_path in ["/server-info", "/agents/snapshot"] {
            assert_eq!(
                request(
                    Method::GET,
                    background_path,
                    &phone.token,
                    ios,
                    Some("background")
                )
                .await,
                StatusCode::OK,
            );
            for _ in 0..7 {
                assert_eq!(
                    request(
                        Method::GET,
                        "/server-info",
                        &phone.token,
                        ios,
                        Some("foreground")
                    )
                    .await,
                    StatusCode::OK,
                );
            }
            for _ in 0..6 {
                assert_eq!(
                    request(
                        Method::POST,
                        "/devices",
                        &phone.token,
                        ios,
                        Some("foreground")
                    )
                    .await,
                    StatusCode::OK,
                );
            }
        }
        assert_eq!(
            request(
                Method::GET,
                "/server-info",
                &phone.token,
                ios,
                Some("unknown")
            )
            .await,
            StatusCode::OK,
        );
        let proof_code = app
            .fleet
            .issue("watcher proof".into(), crate::pairing::Scope::Limited, 60)
            .await
            .unwrap()
            .unwrap();
        let proof_phone = app
            .fleet
            .exchange(proof_code.code, String::new())
            .await
            .unwrap()
            .unwrap();
        let old_ios = "ios/3099;contract=87";
        place.clock.advance_ms(-240_000);
        for _ in 0..6 {
            assert_eq!(
                request(
                    Method::GET,
                    "/server-info",
                    &proof_phone.token,
                    old_ios,
                    None
                )
                .await,
                StatusCode::OK,
            );
        }
        place.clock.advance_ms(240_000);
        let counts = app.experience.relaunches(since, now);
        assert!(
            !app.experience
                .relaunches(now - 180_000, now)
                .contains_key(&proof_phone.device_id)
        );
        assert_eq!(counts[&phone.device_id].foreground_sessions, 2);
        assert_eq!(counts[&phone.device_id].legacy_requests, 0);
        let proof = &counts[&proof_phone.device_id];
        assert_eq!((proof.foreground_sessions, proof.legacy_requests), (0, 6));
        assert_eq!(proof.platform, "ios");
        assert_eq!((proof.build.as_str(), proof.contract), ("3099", Some(87)));
        let paired = crate::edge::Principal::Device {
            device_id: phone.device_id.clone(),
            scope,
        };
        let mut header = axum::http::HeaderMap::new();
        header.insert("x-clarp-client", "ios/200 contract=130".parse().unwrap());
        crate::client_events::record(&app, &paired, &header, &[
            serde_json::json!({"event": "ios.push.background-sync", "level": "error"}),
            serde_json::json!({"event": "metrickit_report", "level": "fault", "detail": {"crashes": 0, "hangs": 12}}),
        ]).await.unwrap();
        let seen = collect(&app, &settings, since, now).await;
        assert!(
            !seen
                .found
                .keys()
                .any(|(kind, _)| kind.starts_with("app_crash_"))
        );

        crate::client_events::record(&app, &paired, &header, &[
            serde_json::json!({"event": "metrickit_report", "level": "fault", "detail": {"crashes": 2, "hangs": 2}}),
            serde_json::json!({"event": "ios.crash", "level": "fatal"}),
        ]).await.unwrap();
        let facts = AgentFacts {
            window: Window {
                reads: vec![Read {
                    waited_ms: 90_000,
                    steered: true,
                }],
                failed: 0,
            },
            queue: vec![Waiting {
                turn: TurnId(1),
                message_id: "u-wait".into(),
                client_id: "wait".into(),
                told_id: "wait".into(),
                text: "Queued".into(),
                speak: false,
                accepted_at_ms: now - 125_000,
                admission: Admission::Later,
                status: QueueItemStatus::Queued,
                origin: Origin::User,
                sender_agent_id: None,
            }],
            parked: None,
            paused: true,
        };

        let mut book = Book::default();
        let mut seen = collect(&app, &settings, since, now).await;
        agent(&mut seen, &settings, agent_id, now, &facts, 0);
        book.reconcile(seen, &settings, &directory, &zone, now);
        assert_eq!(book.rows.len(), 3);
        assert_eq!(
            book.owed.len(),
            4,
            "the desk is not told twice as queue owner"
        );
        let crash = book
            .rows
            .iter()
            .find(|row| row.kind == "app_crash_ios")
            .unwrap();
        assert_eq!(crash.subject, phone.device_id);
        let label = format!("iPhone ({})", phone.device_id);
        let opened = format!("opened for {label} at 2026-11-22 16:23 +01:00");
        for owed in book
            .owed
            .iter()
            .filter(|owed| owed.text.contains("app_crash_ios"))
        {
            assert!(owed.text.contains(&opened), "{}", owed.text);
        }
        // Old stored rows can still pick up the device's current paired name.
        let mut previous_row = serde_json::to_value(crash).unwrap();
        previous_row.as_object_mut().unwrap().remove("device_name");
        let previous_row = serde_json::from_value(previous_row).unwrap();
        let shown = super::super::render(vec![previous_row], &settings, &directory, &zone, false);
        assert_eq!(shown.exit, 0);
        assert!(shown.stdout.contains(&label));
        assert!(shown.stdout.contains("since 2026-11-22 16:23 +01:00"));
        let shown = super::super::render(book.rows.clone(), &settings, &directory, &zone, true);
        assert_eq!(shown.exit, 0);
        let rows: Vec<serde_json::Value> = serde_json::from_str(&shown.stdout).unwrap();
        let shown = rows
            .iter()
            .find(|row| row["kind"] == "app_crash_ios")
            .unwrap();
        assert_eq!(shown["subject"], phone.device_id);
        assert_eq!(shown["device_name"], "iPhone");
        assert_eq!(shown["first_seen"], now);
        assert_eq!(shown["first_seen_local"], "2026-11-22 16:23 +01:00");
        assert!(crash.detail.contains("3 crash events"));
        assert!(crash.detail.contains("2 observed foreground sessions"));
        assert!(crash.detail.contains("0 unmarked requests"));
        assert!(crash.detail.contains("Build 200"));
        let wait = book.rows.iter().find(|row| row.kind == "wait").unwrap();
        assert!(wait.detail.contains("1 steered"));
        assert!(wait.detail.contains("1 still waiting"));
        assert_eq!(&wait.subject, agent_id);
        assert!(book.owed.iter().any(|owed| {
            owed.text
                .contains(&format!("wait opened for {agent_label}"))
        }));
        assert!(book.owed.iter().any(|owed| {
            owed.text
                .contains(&format!("queue opened for {agent_label}"))
        }));
        let mut historical_wait = serde_json::to_value(wait).unwrap();
        for key in ["agent_id", "agent_name", "agent_session"] {
            historical_wait.as_object_mut().unwrap().remove(key);
        }
        let historical_wait = serde_json::from_value(historical_wait).unwrap();
        let shown =
            super::super::render(vec![historical_wait], &settings, &directory, &zone, false);
        assert!(shown.stdout.contains(agent_label));
        let wait_json = rows.iter().find(|row| row["kind"] == "wait").unwrap();
        assert_eq!(wait_json["agent_name"], watched.persona);
        assert_eq!(wait_json["agent_session"], watched.session);
        assert!(
            book.owed
                .iter()
                .all(|owed| matches!(owed.notice, Notice::Open(_)))
        );
        for owed in std::mem::take(&mut book.owed) {
            book.told(owed);
        }

        let mut seen = collect(
            &app,
            &settings,
            settings.window_start(now + 1, now),
            now + 1,
        )
        .await;
        agent(&mut seen, &settings, agent_id, now + 1, &facts, 0);
        book.reconcile(seen, &settings, &directory, &zone, now + 1);
        assert!(book.rows.iter().all(|row| row.count == 2));
        assert!(book.owed.is_empty());

        assert!(app.fleet.revoke(phone.device_id.clone()).await.unwrap());
        assert!(app.fleet.remove(watched.session.clone()).await.unwrap());
        let directory = app.fleet.directory();
        let later = now + 3_600_000;
        let seen = collect(&app, &settings, settings.window_start(later, later), later).await;
        book.reconcile(seen, &settings, &directory, &zone, later);
        assert!(book.rows.iter().all(|row| row.cleared_at == Some(later)));
        let summaries = book
            .owed
            .iter()
            .filter(|owed| matches!(owed.notice, Notice::Summary(_)))
            .collect::<Vec<_>>();
        assert_eq!(summaries.len(), 1);
        assert_eq!(summaries[0].to, "desk");
        assert!(
            summaries[0]
                .text
                .contains("3 opened, 3 cleared, 0 still open")
        );
        assert_eq!(book.owed.len(), 5);
        assert!(book.owed.iter().any(|owed| {
            owed.text
                .contains(&format!("wait cleared for {agent_label}"))
        }));
        let cleared = format!("cleared for {label} at 2026-11-22 17:23 +01:00");
        for owed in book
            .owed
            .iter()
            .filter(|owed| owed.text.contains("app_crash_ios"))
        {
            assert!(owed.text.contains(&cleared), "{}", owed.text);
            assert!(owed.text.contains("open since 2026-11-22 16:23 +01:00"));
        }
        for owed in std::mem::take(&mut book.owed) {
            book.told(owed);
        }
        let crash = book
            .rows
            .iter()
            .find(|row| row.kind == "app_crash_ios")
            .unwrap();
        assert_eq!(crash.notified_open, ["ios-owner", "desk"]);
        assert_eq!(crash.notified_clear, ["ios-owner", "desk"]);

        book.reconcile(Seen::default(), &settings, &directory, &zone, later + 1);
        assert!(book.owed.is_empty());

        let after_hour = later + 3_600_000;
        let mut delayed = Book::default();
        delayed.reconcile(Seen::default(), &settings, &directory, &zone, after_hour);
        assert!(delayed.owed.is_empty(), "a missed hour is not caught up");

        let next_day = later + 24 * 3_600_000;
        delayed.reconcile(Seen::default(), &settings, &directory, &zone, next_day);
        assert_eq!(delayed.owed.len(), 1);
        assert!(matches!(delayed.owed[0].notice, Notice::Summary(_)));
        delayed.reconcile(
            Seen::default(),
            &settings,
            &directory,
            &zone,
            next_day + 3_600_000,
        );
        assert!(
            delayed.owed.is_empty(),
            "a retry expires after the summary hour"
        );

        // A broken header is still counted, but only crashes can open a finding.
        place.clock.advance_ms(after_hour - now);
        let broken = "ios/3099 contract=broken";
        for _ in 0..2 {
            assert_eq!(
                request(
                    Method::GET,
                    "/server-info",
                    &proof_phone.token,
                    broken,
                    None
                )
                .await,
                StatusCode::OK,
            );
        }
        let since = settings.window_start(after_hour, after_hour);
        let counts = app.experience.relaunches(since, after_hour);
        let proof = &counts[&proof_phone.device_id];
        assert_eq!(
            (proof.platform.as_str(), proof.legacy_requests),
            ("unknown", 2)
        );
        assert!(
            collect(&app, &settings, since, after_hour)
                .await
                .found
                .is_empty()
        );
        let paired = crate::edge::Principal::Device {
            device_id: proof_phone.device_id.clone(),
            scope: crate::pairing::Scope::Limited,
        };
        header.insert("x-clarp-client", broken.parse().unwrap());
        let event = serde_json::json!({"event": "ios.crash", "level": "fatal"});
        crate::client_events::record(
            &app,
            &paired,
            &header,
            &[event.clone(), event.clone(), event],
        )
        .await
        .unwrap();
        let seen = collect(&app, &settings, since, after_hour).await;
        assert!(
            seen.found[&("app_crash_unknown".into(), proof_phone.device_id)]
                .contains("3 crash events"),
        );
        assert_eq!(app.experience.malformed_clients.lock().unwrap().len(), 1);
    }
}
