//! Observe one window of Host state. Refused reads leave that finding open.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;
use std::time::Duration;

use clarp_actor::{QueueItemStatus, Waiting};
use clarp_kernel::experience::{Window, is_user_wait};
use clarp_store_sqlite::client_events::CrashCandidate;
use futures_util::{StreamExt as _, stream};
use jiff::tz::TimeZone;
use serde_json::Value;

use crate::edge::App;
use crate::fleet::{Directory, Member};

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

fn watched(directory: &Directory) -> impl Iterator<Item = &Arc<Member>> {
    directory
        .members
        .iter()
        .chain(&directory.janitors)
        .filter(|member| !member.record.is_test && member.record.archived_at_ms.is_none())
}

fn cards(seen: &mut Seen, settings: &Settings, directory: &Directory, now: i64) {
    let mut count = 0;
    let mut oldest = 0;
    let mut agents: BTreeSet<String> = BTreeSet::new();
    for member in watched(directory) {
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

/// The finding kinds of the apps' chat-view scroll flashes, one for each platform (family
/// `scroll_flash`).
const SCROLL_FLASH_KINDS: [&str; 4] = [
    "scroll_flash_ios",
    "scroll_flash_android",
    "scroll_flash_desktop",
    "scroll_flash_web",
];

/// Whether an app's scroll flash is counted: a phone app's (`ios`, `android`) only from a
/// released build number, all digits and at least four of them. The iOS app's debug and
/// simulator builds report `1`, and jumps are made on purpose on one while it is worked on; a
/// test build that names itself (`1112-synthetic`, `1152-debug`) is no released build either. A
/// device whose only flashes came from such builds has no finding, so a finding it had clears on
/// the next pass. From Android build 1154 (app commit 7d90af77) on, the Android app marks a build
/// that is not a release (`<code>-debug`, `<code>-emulator`), so only its release builds count;
/// an older Android build reports its bare version code (the commit count) from every build type,
/// debug and emulator builds included, and its flashes count from every build. The desktop and
/// web apps report versions such as `0.1.0`, not build numbers: their flashes count as they come.
pub(super) fn counted(client: &str, build: &str) -> bool {
    !matches!(client, "ios" | "android")
        || (build.len() >= 4 && build.bytes().all(|sign| sign.is_ascii_digit()))
}

/// How long a device's scroll-flash finding stays open after its newest flash.
pub(super) const FLASH_OPEN_MS: i64 = 24 * 60 * 60 * 1000;

/// One chat-view scroll flash a device reported within the last 24 hours is a finding of that
/// device (`scroll_flash_<platform>`); it clears 24 hours after the device's newest flash.
fn scroll_flashes(seen: &mut Seen, by_device: &crate::client_events::ByDevice, zone: &TimeZone) {
    let hours = |ms: i64| {
        jiff::Timestamp::from_millisecond(ms)
            .map(|at| at.to_zoned(zone.clone()).strftime("%H:%M").to_string())
            .unwrap_or_default()
    };
    for ((device, client), flashes) in by_device {
        if flashes.count == 0 || !platform(client) {
            continue;
        }
        let causes = if flashes.causes.is_empty() {
            "no cause reported".to_owned()
        } else {
            flashes.causes.join(", ")
        };
        let plural = if flashes.count == 1 { "" } else { "es" };
        seen.add(
            &format!("scroll_flash_{client}"),
            device,
            format!(
                "{} chat-view scroll flash{plural} since {} on {client}; the newest at {}: {}, with {causes}.",
                flashes.count,
                hours(flashes.first_at),
                hours(flashes.newest_at),
                flashes.rule,
            ),
        );
    }
}

pub(super) async fn collect(app: &App, settings: &Settings, since: i64, now: i64) -> Seen {
    let directory = app.fleet.directory();
    let mut seen = Seen::default();
    let refusals = app.experience.refusals(since, now);

    let members: Vec<_> = watched(&directory).cloned().collect();
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
        if let Some(since) = view.stalled_since {
            seen.add("stall", id, format!(
                "The running tool has gone silent with a user message queued. No output since {}. Use Stop to release the turn.",
                super::clock(since, &jiff::tz::TimeZone::system()),
            ));
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

    // A device's flashes of the last 24 hours, whatever the pass's window: the finding stays
    // open until a day has passed without one.
    let since = now.saturating_sub(FLASH_OPEN_MS);
    match crate::client_events::flashes(app, since, now, counted).await {
        Ok(by_device) => scroll_flashes(&mut seen, &by_device, &TimeZone::system()),
        Err(why) => {
            seen.unavailable_kinds.extend(SCROLL_FLASH_KINDS);
            eprintln!("clarp-host: experience-watch could not read scroll flashes: {why}");
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

    #[test]
    fn a_devices_flash_opens_tells_at_most_hourly_and_clears_after_a_quiet_day() {
        use super::super::book::{Book, Notice};
        let settings = Settings::from_section(Some(
            &toml::from_str("front_desk = 'desk'\n[owners]\nscroll_flash_ios = 'ios-owner'\n")
                .unwrap(),
        ));
        let directory = Directory::default();
        let zone = TimeZone::UTC;
        let start = 1_800_000_000_000;
        let minute = 60_000;
        let pass = |book: &mut Book, at: i64, detail: Option<&str>| {
            let mut seen = Seen::default();
            if let Some(detail) = detail {
                seen.add("scroll_flash_ios", "iphone", detail.to_owned());
            }
            book.owed.clear();
            book.reconcile(seen, &settings, &directory, &zone, at);
            // The daily summary is no notice of this finding.
            book.owed
                .iter()
                .filter(|owed| !matches!(owed.notice, Notice::Summary(_)))
                .map(|owed| (owed.to.clone(), owed.notice.clone()))
                .collect::<Vec<_>>()
        };
        let mut book = Book::default();
        let opened = pass(&mut book, start, Some("1 flash"));
        assert_eq!(opened.len(), 2);
        assert!(
            opened
                .iter()
                .all(|(_, notice)| matches!(notice, Notice::Open(_)))
        );
        assert_eq!(opened[0].0, "ios-owner");
        // A new flash ten minutes later updates the finding and tells nobody yet.
        assert!(pass(&mut book, start + 10 * minute, Some("2 flashes")).is_empty());
        assert!(pass(&mut book, start + 30 * minute, Some("2 flashes")).is_empty());
        // An hour after the last notice, what changed is told once.
        let told = pass(&mut book, start + 61 * minute, Some("2 flashes"));
        assert_eq!(told.len(), 2);
        assert!(
            told.iter()
                .all(|(_, notice)| matches!(notice, Notice::Update(_, _)))
        );
        assert!(
            pass(&mut book, start + 125 * minute, Some("2 flashes")).is_empty(),
            "nothing new"
        );
        // A day without a flash: the 24-hour reading no longer has the device, and it clears.
        let cleared = pass(&mut book, start + 26 * 60 * minute, None);
        assert!(
            cleared
                .iter()
                .all(|(_, notice)| matches!(notice, Notice::Clear(_)))
        );
        assert_eq!(cleared.len(), 2);
    }

    #[test]
    fn a_phone_apps_flashes_open_a_finding_only_from_a_released_build() {
        let row = |device: &str, client: &str, build: &str, at| {
            clarp_store_sqlite::client_events::Flash {
                device: device.into(),
                client: client.into(),
                client_build: build.into(),
                received_at: at,
                detail: Some(r#"{"rule":"jump_back","events":[{"kind":"reload"}]}"#.into()),
            }
        };
        let rows = [
            // The iOS simulator's debug build: jumps made on purpose.
            row("sim", "ios", "1", 1_800_000_000_000),
            // An Android build that is not a release, once the app marks it so.
            row("emulator", "android", "1152-debug", 1_800_000_000_000),
            // A test build that names itself, not a released build.
            row("synthetic", "android", "1112-synthetic", 1_800_000_000_000),
            // The desktop reports its version, and counts as before.
            row("laptop", "desktop", "0.1.0", 1_800_000_000_000),
            // Released builds: TestFlight and Android release.
            row("iphone", "ios", "1342", 1_800_000_000_000),
            row("pixel", "android", "20261125", 1_800_000_000_000),
            // A device that ran both: only its released build's flash is counted.
            row("ipad", "ios", "1", 1_800_000_000_000),
            row("ipad", "ios", "1342", 1_800_000_600_000),
        ];
        let by = crate::client_events::flashes_by_device(&rows, counted);
        let mut seen = Seen::default();
        scroll_flashes(&mut seen, &by, &TimeZone::UTC);
        let found: Vec<_> = seen.found.keys().cloned().collect();
        let key = |kind: &str, device: &str| (kind.to_owned(), device.to_owned());
        assert_eq!(
            found,
            [
                key("scroll_flash_android", "pixel"),
                key("scroll_flash_desktop", "laptop"),
                key("scroll_flash_ios", "ipad"),
                key("scroll_flash_ios", "iphone"),
            ]
        );
        assert!(seen.found[&key("scroll_flash_ios", "ipad")].starts_with("1 chat-view"));
        for build in ["1", "123", "", "1342a", "1112-synthetic", "1152-debug"] {
            assert!(
                !counted("ios", build) && !counted("android", build),
                "{build:?}"
            );
        }
        for build in ["1000", "1342", "20261125"] {
            assert!(
                counted("ios", build) && counted("android", build),
                "{build:?}"
            );
        }
        // The desktop and web apps report versions, and their flashes count as before.
        assert!(counted("desktop", "0.1.0") && counted("web", "1"));
    }

    #[test]
    fn one_scroll_flash_is_a_finding_of_its_device_and_goes_to_the_apps_owner() {
        let mut seen = Seen::default();
        let mut by = crate::client_events::ByDevice::new();
        let flashes = |count, rule: &str, causes: &[&str]| crate::client_events::Flashes {
            count,
            first_at: 1_800_000_000_000,
            newest_at: 1_800_000_600_000,
            rule: rule.into(),
            causes: causes.iter().map(|c| (*c).to_owned()).collect(),
        };
        let key = |device: &str, client: &str| (device.to_owned(), client.to_owned());
        by.insert(
            key("iphone", "ios"),
            flashes(1, "jump_back", &["reload", "height"]),
        );
        by.insert(key("ipad", "ios"), flashes(3, "top_flip", &[]));
        by.insert(key("tv", "tv"), flashes(2, "jump_back", &[]));
        scroll_flashes(&mut seen, &by, &TimeZone::UTC);
        assert_eq!(
            seen.found[&("scroll_flash_ios".into(), "iphone".into())],
            "1 chat-view scroll flash since 08:00 on ios; the newest at 08:10: jump_back, with reload, height."
        );
        assert_eq!(
            seen.found[&("scroll_flash_ios".into(), "ipad".into())],
            "3 chat-view scroll flashes since 08:00 on ios; the newest at 08:10: top_flip, with no cause reported."
        );
        assert_eq!(
            seen.found.len(),
            2,
            "a client that is no platform is not a finding"
        );
        let settings = Settings::from_section(Some(
            &toml::from_str(
                "front_desk = 'desk'\n[owners]\nscroll_flash_ios = 'ios-owner'\napp_crash = 'apps'\n",
            )
            .unwrap(),
        ));
        assert_eq!(
            settings.recipients("scroll_flash_ios"),
            ["ios-owner", "desk"]
        );
        assert_eq!(
            settings.recipients("scroll_flash_android"),
            ["apps", "desk"]
        );
        let family = Settings::from_section(Some(
            &toml::from_str(
                "front_desk = 'desk'\n[owners]\nscroll_flash = 'ui'\napp_crash_ios = 'ios'\n",
            )
            .unwrap(),
        ));
        assert_eq!(family.recipients("scroll_flash_ios"), ["ui", "desk"]);
    }

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
                    read_together: false,
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
    async fn a_silent_tool_with_queued_user_words_marks_and_clears_the_live_stall() {
        use axum::extract::ConnectInfo;
        use clarp_kernel::conversation::Message;
        use clarp_ports::clock::{Clock, SystemClock};
        use clarp_protocol::requests::CreateAgentRequest;
        use clarp_spool::{Entry, ShimEvent, SpoolReader};

        // The shim is built into the profile's folder (`target/debug`). Where this test's own
        // binary is depends on cargo: `target/debug/deps` here, and under the newer build
        // layout of GitHub's runners `target/debug/build/clarp-host/<hash>/out` (23 Nov 2026,
        // where two folders up was no shim and the turn never started). So it is looked for
        // in every folder above this binary.
        let exe = std::env::current_exe().unwrap();
        let shim = exe
            .ancestors()
            .skip(1)
            .map(|dir| dir.join("clarp-shim"))
            .find(|shim| shim.is_file())
            .unwrap_or_else(|| panic!("no clarp-shim built above {}", exe.display()));
        let place = crate::unkept::at_the_port::place_with_shim("stalled-turn", shim);
        let mut app = place.app().await;
        std::sync::Arc::make_mut(&mut app.config).auth_token.clear();
        place
            .clock
            .advance_ms(SystemClock.now_ms() - app.clock.now_ms());
        let worker = app
            .fleet
            .create(CreateAgentRequest {
                name: "Silent Worker".into(),
                backend: Some("scripted".into()),
                ..CreateAgentRequest::default()
            })
            .await
            .unwrap()
            .unwrap();
        let handle = app
            .fleet
            .directory()
            .by_id(&worker.agent_id)
            .unwrap()
            .agent
            .clone();
        let (_, mut events) = app.hub.subscribe(None);
        handle
            .send(
                Message {
                    id: "u-blocking".into(),
                    // Long enough to be stalled and seen so on a loaded runner; its end clears it.
                    text: "sleep 8".into(),
                    ..Message::default()
                },
                Admission::Queue,
            )
            .await
            .unwrap();

        let spool = app
            .config
            .data_dir
            .join("agents")
            .join(&worker.agent_id)
            .join("spool/1.jsonl");
        // The waits are for a condition: a wide bound costs nothing when it is met, and a loaded
        // runner (GitHub's four cores, 22 Nov 2026) took more than two seconds to spawn.
        let started_at = tokio::time::timeout(Duration::from_secs(20), async {
            let reader = SpoolReader::new(&spool);
            loop {
                let running = handle
                    .view()
                    .status
                    .live
                    .as_ref()
                    .is_some_and(|live| !live.starting);
                if running
                    && let Ok(window) = reader.read_window(0, u64::MAX, 4096)
                    && let Some(first) = window.records.iter().find(|record| {
                        matches!(record.entry, Entry::Shim(ShimEvent::Spawned { .. }))
                    })
                {
                    break first.t;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();

        let due = started_at + app.config.stalled_turn_ms;
        place.clock.advance_ms(due - 120 - app.clock.now_ms());
        handle
            .send(
                Message {
                    id: "u-followup".into(),
                    text: "Please come back".into(),
                    origin: Origin::Trigger {
                        name: "oracle".into(),
                    },
                    ..Message::default()
                },
                Admission::Later,
            )
            .await
            .unwrap();
        assert!(handle.view().stalled_since.is_none());
        place.clock.advance_ms(120);

        let entered = tokio::time::timeout(Duration::from_secs(20), async {
            loop {
                let event: Value =
                    serde_json::from_str(&events.recv().await.unwrap().json).unwrap();
                if event["type"] == "agent-activity"
                    && event["agent_id"] == worker.agent_id
                    && event["stalled_action"] == "stop"
                {
                    break event;
                }
            }
        })
        .await
        .unwrap();
        assert!(started_at > 0);
        let records = SpoolReader::new(&spool)
            .read_window(0, u64::MAX, 4096)
            .unwrap();
        assert!(
            !records.records.iter().any(|record| {
                matches!(&record.entry, Entry::Out(text) | Entry::Err(text) if !text.is_empty())
            }),
            "the tool was silent from launch"
        );
        assert_eq!(entered["stalled_since"], started_at);
        let request = Request::builder()
            .uri("/agents/snapshot")
            .extension(ConnectInfo(
                "127.0.0.1:24000".parse::<std::net::SocketAddr>().unwrap(),
            ))
            .body(Body::empty())
            .unwrap();
        let response = crate::edge::router(app.clone())
            .oneshot(request)
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let snapshot: Value = serde_json::from_slice(
            &axum::body::to_bytes(response.into_body(), 512 * 1024)
                .await
                .unwrap(),
        )
        .unwrap();
        let row = snapshot["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["agent_id"] == worker.agent_id)
            .unwrap();
        for shown in [row, &row["activity"], &row["live_activity"]] {
            assert_eq!(shown["stalled_since"], started_at);
            assert_eq!(shown["stalled_action"], "stop");
        }
        let settings = Settings::from_section(Some(
            &toml::from_str("front_desk = 'desk'\n[owners]\nqueue = 'queue-owner'\n").unwrap(),
        ));
        let seen = collect(&app, &settings, started_at, due).await;
        assert!(
            seen.found
                .contains_key(&("stall".into(), worker.agent_id.clone()))
        );
        assert_eq!(settings.recipients("stall"), ["queue-owner", "desk"]);

        let cleared = tokio::time::timeout(Duration::from_secs(30), async {
            loop {
                let event: Value =
                    serde_json::from_str(&events.recv().await.unwrap().json).unwrap();
                if event["type"] == "agent-activity"
                    && event["agent_id"] == worker.agent_id
                    && event.get("stalled_since") == Some(&Value::Null)
                {
                    break event;
                }
            }
        })
        .await
        .unwrap();
        assert!(cleared["stalled_action"].is_null());
        assert!(handle.view().stalled_since.is_none());
        let seen = collect(&app, &settings, started_at, due).await;
        assert!(!seen.found.contains_key(&("stall".into(), worker.agent_id)));
    }

    #[tokio::test]
    async fn experience_watch_opens_waits_only_for_ordinary_active_agents() {
        use axum::extract::ConnectInfo;
        use clarp_kernel::{control::Control, conversation::Message};
        use serde_json::json;

        let place = crate::unkept::at_the_port::place("experience-agent-test-flag");
        let mut app = place.app().await;
        std::sync::Arc::make_mut(&mut app.config).auth_token = "fixture-admin".into();
        let router = crate::edge::router(app.clone());
        let post = |uri: &str, body: Value| {
            let body = body.to_string();
            let request = Request::post(uri)
                .extension(ConnectInfo(std::net::SocketAddr::from((
                    [127, 0, 0, 1],
                    24000,
                ))))
                .header(header::AUTHORIZATION, "Bearer fixture-admin")
                .header(header::CONTENT_TYPE, "application/json")
                .header(header::CONTENT_LENGTH, body.len())
                .body(Body::from(body))
                .unwrap();
            let router = router.clone();
            async move {
                let response = router.oneshot(request).await.unwrap();
                let status = response.status();
                let bytes = axum::body::to_bytes(response.into_body(), 512 * 1024)
                    .await
                    .unwrap();
                assert_eq!(
                    status,
                    StatusCode::OK,
                    "{}",
                    String::from_utf8_lossy(&bytes)
                );
                serde_json::from_slice::<Value>(&bytes).unwrap()
            }
        };

        let normal = post(
            "/agents",
            json!({"name":"Probe Example", "backend":"scripted"}),
        )
        .await;
        let via_http = post(
            "/agents",
            json!({"name":"Elm", "backend":"scripted", "test":true}),
        )
        .await;
        let tool = post(
            "/tools/run",
            json!({
                "tool": "clarp-admin",
                "argv": ["agent", "create", "Oak", "--backend", "scripted", "--test"]
            }),
        )
        .await;
        assert_eq!(tool["exit"], 0, "{tool}");
        let via_cli: Value = serde_json::from_str(tool["stdout"].as_str().unwrap()).unwrap();
        let archived = post("/agents", json!({"name":"Beech", "backend":"scripted"})).await;
        let archived_session = archived["session"].as_str().unwrap().to_owned();
        app.fleet
            .amend(archived_session, crate::fleet::Amend::Archive(true))
            .await
            .unwrap()
            .unwrap();

        let since = app.clock.now_ms();
        let directory = app.fleet.directory();
        let mut expected = Vec::new();
        for (created, watched) in [
            (&normal, true),
            (&via_http, false),
            (&via_cli, false),
            (&archived, false),
        ] {
            assert!(created["agent"].get("test").is_none());
            assert!(created["agent"].get("is_test").is_none());
            let member = directory
                .by_session(created["session"].as_str().unwrap())
                .unwrap();
            member.agent.hold(Control::for_relaunch()).await.unwrap();
            let sent = member
                .agent
                .send(
                    Message {
                        id: "u-waiting-word".into(),
                        text: "Please look at this".into(),
                        origin: Origin::User,
                        ..Message::default()
                    },
                    Admission::Background,
                )
                .await
                .unwrap();
            assert!(sent.queued, "no wait for {}", member.record.session);
            if watched {
                expected.push(member.record.agent_id.clone());
            }
        }

        let settings = Settings::from_section(None);
        place.clock.advance_ms(settings.wait_ms + 1_000);
        let now = app.clock.now_ms();
        let seen = collect(&app, &settings, since, now).await;
        let mut book = Book::default();
        book.reconcile(seen, &settings, &directory, &jiff::tz::TimeZone::UTC, now);
        let waits: Vec<_> = book
            .rows
            .into_iter()
            .filter(|row| row.kind == "wait")
            .map(|row| row.subject)
            .collect();
        assert_eq!(waits, expected);
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
                read_together: false,
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
