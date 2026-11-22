//! What the Host can learn of a process on its machine: whether a worker that registered a
//! background job is still the process it was.
//!
//! A pid alone is not a name: it is given out again. A process is named by its pid and its
//! **start token**, the machine's boot and the moment the process started, exactly as the
//! Python Host names it (`server/lib/background_job_process.py`), so that a job registered
//! under one Host's tool means the same to the other.

/// The start token of a process: empty when there is none with this pid.
pub fn start_token(pid: u32) -> String {
    if pid == 0 {
        return String::new();
    }
    let Ok(boot) = std::fs::read_to_string("/proc/sys/kernel/random/boot_id") else {
        return String::new();
    };
    match after_name(pid).and_then(|fields| fields.get(19).cloned()) {
        Some(started) => format!("{}:{started}", boot.trim()),
        None => String::new(),
    }
}

/// The fields of `/proc/<pid>/stat` after the process's name, which may itself hold spaces
/// and brackets: everything after the last `)`.
fn after_name(pid: u32) -> Option<Vec<String>> {
    let raw = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let (_, tail) = raw.rsplit_once(')')?;
    Some(tail.split_whitespace().map(str::to_string).collect())
}

/// Whether the process that had this pid and this start token is still running.
pub fn is_alive(pid: u32, token: &str) -> bool {
    if token.is_empty() || start_token(pid) != token {
        return false;
    }
    // A zombie has ended; only its entry is still there.
    after_name(pid)
        .and_then(|fields| fields.first().cloned())
        .is_some_and(|state| !state.starts_with('Z') && !state.starts_with('X'))
}

fn command_args(pid: u32) -> Vec<String> {
    use std::io::Read as _;
    let Ok(file) = std::fs::File::open(format!("/proc/{pid}/cmdline")) else {
        return Vec::new();
    };
    let mut raw = Vec::new();
    if file.take(4096).read_to_end(&mut raw).is_err() {
        return Vec::new();
    }
    raw.split(|byte| *byte == 0)
        .filter(|word| !word.is_empty())
        .map(|word| String::from_utf8_lossy(word).into_owned())
        .collect()
}

/// The command line a process was started with, its words joined by spaces.
pub fn command_line(pid: u32) -> String {
    std::fs::read(format!("/proc/{pid}/cmdline"))
        .map(|raw| {
            raw.split(|byte| *byte == 0)
                .filter(|word| !word.is_empty())
                .map(|word| String::from_utf8_lossy(word).into_owned())
                .collect::<Vec<_>>()
                .join(" ")
        })
        .unwrap_or_default()
}

/// Observe the commands beneath the worker for this generation, without
/// reading a log or treating a future shell command as active.
pub(crate) fn worker_commands<T>(
    worker: &clarp_kernel::jobs::Worker,
    step: impl Fn(&[String]) -> Option<T>,
) -> Option<Vec<(T, i64)>> {
    use nix::unistd::{SysconfVar, sysconf};
    use std::collections::VecDeque;
    if !is_alive(worker.pid, &worker.start_token) {
        return None;
    }
    let hz = sysconf(SysconfVar::CLK_TCK).ok()??;
    let uptime = std::fs::read_to_string("/proc/uptime").ok()?;
    let seconds: f64 = uptime.split_whitespace().next()?.parse().ok()?;
    if hz <= 0 || !seconds.is_finite() {
        return None;
    }
    let mut pending = VecDeque::from([(worker.pid, None)]);
    let mut commands = Vec::new();
    // A small, bounded read from a watcher pass. An incomplete tree is
    // insufficient evidence to say its check is holding a release.
    for _ in 0..32 {
        let Some((pid, parent)) = pending.pop_front() else {
            break;
        };
        let fields = after_name(pid)?;
        if let Some(parent) = parent
            && fields.get(1)?.parse::<u32>().ok()? != parent
        {
            return None;
        }
        let started: f64 = fields.get(19)?.parse().ok()?;
        let args = command_args(pid);
        if let Some(step) = step(&args) {
            let elapsed_ms = ((seconds - started / hz as f64) * 1000.0).max(0.0) as i64;
            commands.push((step, elapsed_ms));
            continue;
        }
        let children = std::fs::read_to_string(format!("/proc/{pid}/task/{pid}/children")).ok()?;
        pending.extend(
            children
                .split_whitespace()
                .filter_map(|word| word.parse::<u32>().ok().map(|child| (child, Some(pid)))),
        );
    }
    (pending.is_empty() && is_alive(worker.pid, &worker.start_token)).then_some(commands)
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_process_is_named_by_its_pid_and_when_it_started() {
        let me = std::process::id();
        let token = start_token(me);
        assert!(token.contains(':'), "the boot and the start: {token}");
        assert_eq!(token, start_token(me), "the same every time it is asked");
        assert!(is_alive(me, &token));
        assert!(
            !is_alive(me, "another-boot:1"),
            "another process had this pid"
        );
        assert!(!is_alive(me, ""));
        assert_eq!(start_token(0), "");
        // A pid nobody has: far above what a machine gives out.
        assert_eq!(start_token(4_000_000_000), "");
        assert!(!is_alive(4_000_000_000, &token));
        assert!(!command_line(me).is_empty());
    }

    #[test]
    fn a_process_that_ended_is_not_alive_under_its_old_name() {
        let mut child = std::process::Command::new("sleep")
            .arg("30")
            .spawn()
            .unwrap();
        let pid = child.id();
        let token = start_token(pid);
        assert!(is_alive(pid, &token));
        // A child is its parent's program until it has become its own.
        let became_itself = (0..400).any(|_| {
            let is_sleep = command_line(pid).starts_with("sleep");
            if !is_sleep {
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
            is_sleep
        });
        assert!(became_itself, "{}", command_line(pid));
        child.kill().unwrap();
        // Killed and not yet reaped: a zombie, which is not alive.
        std::thread::sleep(std::time::Duration::from_millis(50));
        assert!(!is_alive(pid, &token));
        child.wait().unwrap();
        assert!(!is_alive(pid, &token));
    }
}
