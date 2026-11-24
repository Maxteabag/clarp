//! How a lane starts the tool it keeps, whatever the tool: in a process group of its own, so the
//! lane ends all of it at once (decision D26), and with a death signal set from its parent, so a
//! tool that does not end when its input closes (OpenCode's `serve` listens on a port) cannot
//! outlive a lane that was killed. Codex's app-server ends with its standard input and is started
//! as before (`codex_app::AppServer::spawn`); a driver whose tool needs it starts it through
//! [`kept`].
//!
//! The death signal is set by `setpriv --pdeathsig`, which then runs the tool in its own place:
//! this crate allows no `unsafe`, which a `pre_exec` of our own would need. The signal follows
//! the thread that started the child, not the process: a lane starts its tool from its main
//! thread, which lives as long as the lane does.

use std::process::Command;

use std::os::unix::process::CommandExt;

/// The signal a kept tool is sent when the lane that started it is gone.
const DEATH_SIGNAL: &str = "TERM";

/// The command that starts `program` with `args` as a lane's kept tool: its own process group,
/// and `SIGTERM` when the lane's starting thread ends. `setpriv` replaces itself with the tool,
/// so the child's pid is the tool's.
#[allow(
    dead_code,
    reason = "the layer's spawn for drivers whose tool outlives its input (issue 761); held by its tests"
)]
pub(super) fn kept(program: &str, args: &[String]) -> Command {
    let mut command = Command::new("setpriv");
    command
        .args(["--pdeathsig", DEATH_SIGNAL, "--"])
        .arg(program)
        .args(args)
        .process_group(0);
    command
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::kept;
    use std::os::unix::process::ExitStatusExt;
    use std::time::{Duration, Instant};

    #[test]
    fn a_kept_tool_ends_with_the_thread_that_started_it() {
        let (send, take) = std::sync::mpsc::channel();
        let starter = std::thread::spawn(move || {
            let child = kept("sleep", &["30".to_owned()]).spawn().unwrap();
            // The thread lives until the signal is set: `setpriv` has become the tool.
            let comm = format!("/proc/{}/comm", child.id());
            let since = Instant::now();
            while std::fs::read_to_string(&comm).unwrap_or_default().trim() != "sleep" {
                assert!(
                    since.elapsed() < Duration::from_secs(10),
                    "setpriv did not run"
                );
                std::thread::sleep(Duration::from_millis(5));
            }
            send.send(child).unwrap();
        });
        let mut child = take.recv().unwrap();
        starter.join().unwrap();
        let since = Instant::now();
        let status = loop {
            if let Some(status) = child.try_wait().unwrap() {
                break status;
            }
            assert!(
                since.elapsed() < Duration::from_secs(10),
                "the tool outlived its lane"
            );
            std::thread::sleep(Duration::from_millis(20));
        };
        assert_eq!(status.signal(), Some(nix::libc::SIGTERM));
    }

    #[test]
    fn a_kept_tool_leads_a_group_of_its_own() {
        let mut child = kept("sleep", &["30".to_owned()]).spawn().unwrap();
        let pid = nix::unistd::Pid::from_raw(i32::try_from(child.id()).unwrap());
        assert_eq!(nix::unistd::getpgid(Some(pid)).unwrap(), pid);
        child.kill().unwrap();
        child.wait().unwrap();
    }
}
