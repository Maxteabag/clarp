//! Observe one window of Host state. Refused reads leave that finding open.

use std::collections::BTreeSet;
use std::time::Duration;

use clarp_actor::{QueueItemStatus, Waiting};
use clarp_kernel::experience::Window;
use clarp_kernel::jobs::Job;
use clarp_kernel::turn::Origin;
use futures_util::{StreamExt as _, stream};

use crate::edge::App;
use crate::fleet::Directory;

use super::{book::Seen, settings::Settings};

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
    for row in facts
        .queue
        .iter()
        .filter(|row| row.origin == Origin::User && row.status != QueueItemStatus::Started)
    {
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
                agents.insert(member.record.session.clone());
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

fn release(seen: &mut Seen, settings: &Settings, job: &Job, now: i64) {
    let age = now.saturating_sub(job.started_at_ms);
    if job.status.is_active()
        && age > settings.release_hold_ms
        && settings.release_kinds.contains(&job.kind)
    {
        seen.add(
            "release",
            &job.id,
            format!(
                "{} has held the release for {} min.",
                job.kind,
                age / 60_000,
            ),
        );
    }
}

pub(super) async fn collect(app: &App, settings: &Settings, now: i64) -> Seen {
    let since = now.saturating_sub(settings.window_ms);
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
            release(&mut seen, settings, job, now);
        }
    }

    cards(&mut seen, settings, &directory, now);
    match app.fleet.computer_jobs().await {
        Ok(jobs) => {
            for job in &jobs {
                release(&mut seen, settings, job, now);
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

    // There is no device client-events journal on this Host yet. The relaunch
    // signal works independently of crash-event collection.
    for (device, launch) in app.experience.relaunches(since, now) {
        if launch.count >= settings.relaunch_burst
            && matches!(
                launch.platform.as_str(),
                "ios" | "android" | "desktop" | "web"
            )
        {
            let kind = format!("app_crash_{}", launch.platform);
            let contract = launch
                .contract
                .map_or_else(|| "unknown".to_owned(), |n| n.to_string());
            seen.add(&kind, &device, format!(
                "{} launch requests. Build {}. Contract {contract}. Crash event store unavailable.",
                launch.count, launch.build,
            ));
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
    use clarp_kernel::experience::Read;
    use clarp_kernel::turn::{Admission, TurnId};

    #[tokio::test]
    async fn experience_watch_findings_open_clear_and_summarize_once() {
        let place = crate::unkept::at_the_port::place("experience-watch-pass");
        let app = place.app().await;
        let local_hour = |ms| {
            jiff::Timestamp::from_millisecond(ms)
                .unwrap()
                .to_zoned(jiff::tz::TimeZone::system())
                .hour()
        };
        let first = app.clock.now_ms();
        let now = (0..24)
            .map(|n| first + n * 3_600_000)
            .find(|ms| (1..21).contains(&local_hour(*ms)))
            .unwrap();
        let config: toml::Value = format!(
            "enabled = true\nsummary_hour = {}\nfront_desk = 'desk'\n[owners]\napp_crash = 'ios-owner'\nqueue = 'desk'\n",
            local_hour(now) + 1,
        ).parse().unwrap();
        let settings = Settings::from_section(Some(&config));
        for n in (0..27).rev() {
            app.experience.launched(
                "paired-phone",
                Some("ios/200 contract=130"),
                now - n * 4_000,
            );
        }
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
        let mut seen = collect(&app, &settings, now).await;
        agent(&mut seen, &settings, "worker", now, &facts, 0);
        book.reconcile(seen, &settings, now);
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
        assert_eq!(crash.subject, "paired-phone");
        assert!(crash.detail.contains("27 launch requests"));
        assert!(crash.detail.contains("Build 200"));
        let wait = book.rows.iter().find(|row| row.kind == "wait").unwrap();
        assert!(wait.detail.contains("1 steered"));
        assert!(wait.detail.contains("1 still waiting"));
        assert!(book.rows.iter().any(|row| row.kind == "queue"));
        assert!(
            book.owed
                .iter()
                .all(|owed| matches!(owed.notice, Notice::Open(_)))
        );
        for owed in std::mem::take(&mut book.owed) {
            book.told(owed);
        }

        let mut seen = collect(&app, &settings, now + 1).await;
        agent(&mut seen, &settings, "worker", now + 1, &facts, 0);
        book.reconcile(seen, &settings, now + 1);
        assert!(book.rows.iter().all(|row| row.count == 2));
        assert!(book.owed.is_empty());

        let later = now + 3_600_000;
        let seen = collect(&app, &settings, later).await;
        book.reconcile(seen, &settings, later);
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

        book.reconcile(Seen::default(), &settings, later + 1);
        assert!(book.owed.is_empty());
    }
}
