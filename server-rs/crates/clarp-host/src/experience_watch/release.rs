//! An opted-in check job needs a live check step before it can hold a release.

use clarp_kernel::jobs::{Job, JobStatus};
use jiff::tz::TimeZone;

use super::{book::Seen, clock, settings::Settings};
use crate::process;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Step {
    Check(&'static str),
    Other,
}

fn name(path: &str) -> &str {
    path.rsplit('/').next().unwrap_or_default()
}

pub(super) fn step(args: &[String]) -> Option<Step> {
    let (command, args) = args.split_first()?;
    let (command, args) = match name(command) {
        "sh" | "bash" | "dash" | "zsh" => {
            let (script, rest) = args.split_first()?;
            (script, rest)
        }
        _ => (command, args),
    };
    let action = args.first().map(String::as_str);
    let host_build = command == "tools/build" || command.ends_with("/tools/build");
    let has = |actions: &[&str]| args.iter().any(|arg| actions.contains(&arg.as_str()));
    Some(match (name(command), action) {
        ("cargo", Some("check")) => Step::Check("cargo check"),
        ("cargo", Some("clippy")) => Step::Check("cargo clippy"),
        ("cargo", Some("test")) => Step::Check("cargo test"),
        ("cargo-clippy", _) => Step::Check("cargo clippy"),
        ("pytest" | "py.test", _) => Step::Check("pytest"),
        ("python" | "python3", Some("-m")) if args.get(1).is_some_and(|arg| arg == "pytest") => {
            Step::Check("python -m pytest")
        }
        ("swift", Some("test")) => Step::Check("swift test"),
        ("xcodebuild", _) if has(&["build", "archive", "install", "build-for-testing"]) => Step::Other,
        ("xcodebuild", _) if has(&["test", "test-without-building"]) => Step::Check("xcodebuild test"),
        ("make", Some("test-critical")) => Step::Check("make test-critical"),
        ("make", Some("test-guards")) => Step::Check("make test-guards"),
        ("final-pass", _) => Step::Check("final-pass"),
        ("build", Some("check")) if host_build => Step::Check("tools/build check"),
        ("build", Some("clippy")) if host_build => Step::Check("tools/build clippy"),
        ("build", Some("test")) if host_build => Step::Check("tools/build test"),
        ("cargo" | "swift" | "xcodebuild" | "release_build.sh" | "install-host" | "install.sh" | "ssh", _) => {
            Step::Other
        }
        ("build", _) if host_build => Step::Other,
        _ => return None,
    })
}

fn eligible(settings: &Settings, job: &Job, now: i64) -> bool {
    job.status == JobStatus::Running
        && matches!(job.kind.as_str(), "check" | "test")
        && settings.release_kinds.contains(&job.kind)
        && now.saturating_sub(job.started_at_ms) > settings.release_hold_ms
}

pub(super) fn collect(seen: &mut Seen, settings: &Settings, job: &Job, now: i64) {
    if !eligible(settings, job, now) {
        return;
    }
    match process::worker_commands(&job.worker, step) {
        Some(commands) => observe(seen, settings, job, &commands, now, &TimeZone::system()),
        None => seen.missing("release", &job.id),
    }
}

pub(super) fn observe(
    seen: &mut Seen,
    settings: &Settings,
    job: &Job,
    commands: &[(Step, i64)],
    now: i64,
    zone: &TimeZone,
) {
    if !eligible(settings, job, now) {
        return;
    }
    let mut active = None;
    for &(step, elapsed) in commands {
        match step {
            Step::Other => return,
            Step::Check(name) => {
                if active.is_none_or(|(_, age)| elapsed > age) {
                    active = Some((name, elapsed));
                }
            }
        }
    }
    let Some((name, elapsed)) = active else { return };
    let started = job.started_at_ms.max(now.saturating_sub(elapsed));
    let held = now.saturating_sub(started);
    if held > settings.release_hold_ms {
        seen.add(
            "release",
            &job.id,
            format!(
                "Registered kind {}. Active step {name} has held the release for {} min, since {}.",
                job.kind, held / 60_000, clock(started, zone),
            ),
        );
    }
}
