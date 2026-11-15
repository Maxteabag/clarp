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
        assert!(command_line(pid).starts_with("sleep"));
        child.kill().unwrap();
        // Killed and not yet reaped: a zombie, which is not alive.
        std::thread::sleep(std::time::Duration::from_millis(50));
        assert!(!is_alive(pid, &token));
        child.wait().unwrap();
        assert!(!is_alive(pid, &token));
    }
}
