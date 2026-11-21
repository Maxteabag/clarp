//! Stop a streaming process only after its own shim has gone.
//!
//! The tool is the leader of a separate group in the shim's session. A group can
//! survive the shim. Signal its members through pidfds so a recycled numeric PID
//! cannot receive the signal, then wait until nobody in the old group can read.

use std::io::{self, ErrorKind};
use std::time::{Duration, Instant};

use clarp_kernel::steering::InputOwner;
use rustix::process::{Pid, PidfdFlags, Signal, pidfd_open, pidfd_send_signal};

#[derive(Clone, Copy, PartialEq, Eq)]
struct Process {
    pid: u32,
    group: u32,
    session: u32,
    start: u64,
    ended: bool,
}

fn process(pid: u32) -> io::Result<Option<Process>> {
    let stat = match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Ok(stat) => stat,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let parsed = || {
        let mut fields = stat.rsplit_once(')')?.1.split_whitespace();
        let ended = matches!(fields.next()?, "Z" | "X");
        let group = fields.nth(1)?.parse().ok()?;
        let session = fields.next()?.parse().ok()?;
        let start = fields.nth(15)?.parse().ok()?;
        Some(Process { pid, group, session, start, ended })
    };
    parsed().map(Some).ok_or_else(|| io::Error::new(ErrorKind::InvalidData, "invalid process stat"))
}

fn start_on_boot(token: &str, boot: &str) -> io::Result<Option<u64>> {
    let (recorded, start) = token.split_once(':')
        .ok_or_else(|| io::Error::new(ErrorKind::InvalidData, "invalid process start token"))?;
    if recorded != boot {
        return Ok(None);
    }
    start.parse().map(Some).map_err(|_| io::Error::new(ErrorKind::InvalidData, "invalid process start time"))
}

fn kill_exact(member: Process) -> io::Result<()> {
    let pid = i32::try_from(member.pid).ok().and_then(Pid::from_raw)
        .ok_or_else(|| io::Error::new(ErrorKind::InvalidData, "invalid tool pid"))?;
    let fd = match pidfd_open(pid, PidfdFlags::empty()) {
        Ok(fd) => fd,
        Err(rustix::io::Errno::SRCH) => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    if process(member.pid)? != Some(member) {
        return Ok(());
    }
    match pidfd_send_signal(&fd, Signal::KILL) {
        Ok(()) | Err(rustix::io::Errno::SRCH) => Ok(()),
        Err(error) => Err(error.into()),
    }
}

fn members(owner: &InputOwner, tool_start: u64) -> io::Result<Vec<Process>> {
    let mut found = Vec::new();
    for entry in std::fs::read_dir("/proc")? {
        let Some(pid) = entry?.file_name().to_str().and_then(|name| name.parse::<u32>().ok()) else {
            continue;
        };
        if let Some(member) = process(pid)?
            && member.session == owner.shim_pid
            && member.group == owner.group
            && member.start >= tool_start
            && !member.ended
        {
            found.push(member);
        }
    }
    Ok(found)
}

/// True once the old tool cannot read the pipe. False means the recorded shim still owns it.
pub(super) fn ended(owner: &InputOwner) -> io::Result<bool> {
    if owner.group == 0 || owner.group != owner.tool_pid || owner.shim_pid == 0 {
        return Err(io::Error::new(ErrorKind::InvalidData, "invalid streaming process group"));
    }
    let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
    let boot = boot.trim();
    let (Some(shim_start), Some(tool_start)) = (
        start_on_boot(&owner.shim_start, boot)?,
        start_on_boot(&owner.tool_start, boot)?,
    ) else {
        return Ok(true);
    };
    if let Some(shim) = process(owner.shim_pid)? {
        if shim.start != shim_start {
            // The old session ID could only be reused after its last member went.
            return Ok(true);
        }
        if !shim.ended {
            return Ok(false);
        }
    }
    if process(owner.tool_pid)?.is_some_and(|tool| tool.start != tool_start) {
        // Likewise the old group ID remains reserved as long as it has a member.
        return Ok(true);
    }
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let mut group = members(owner, tool_start)?;
        if group.is_empty() {
            return Ok(true);
        }
        // The recorded leader must still identify this group after the scan. If it has
        // gone, matching numeric IDs alone cannot authorize killing any residual member.
        if process(owner.tool_pid)?.is_some_and(|tool| {
            tool.start == tool_start && tool.session == owner.shim_pid && tool.group == owner.group
        }) {
            group.sort_by_key(|member| member.pid == owner.tool_pid);
            for member in group {
                kill_exact(member)?;
            }
        }
        if Instant::now() >= deadline {
            return Err(io::Error::new(ErrorKind::TimedOut, "orphaned tool group did not exit"));
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::process::{Command, Stdio};

    #[test]
    fn a_lost_shim_is_fenced_by_identity_before_its_tool_can_be_recovered() {
        let code = "import os, subprocess, time\n\
                    os.setsid()\n\
                    tool = subprocess.Popen(['sleep', '30'], process_group=0)\n\
                    print(tool.pid, flush=True)\n\
                    time.sleep(30)\n";
        let mut shim = Command::new("python3").args(["-c", code])
            .stdout(Stdio::piped()).spawn().unwrap();
        let mut line = String::new();
        BufReader::new(shim.stdout.take().unwrap()).read_line(&mut line).unwrap();
        let tool_pid = line.trim().parse().unwrap();
        let tool = process(tool_pid).unwrap().unwrap();
        let shim_pid = shim.id();
        let boot = std::fs::read_to_string("/proc/sys/kernel/random/boot_id").unwrap();
        let token = |start| format!("{}:{start}", boot.trim());
        let owner = InputOwner {
            shim_pid,
            shim_start: token(process(shim_pid).unwrap().unwrap().start),
            tool_pid,
            tool_start: token(tool.start),
            group: tool.group,
            projects: "/fixture/projects".into(),
        };
        assert!(!ended(&owner).unwrap(), "the live shim stays in charge");
        shim.kill().unwrap();
        shim.wait().unwrap();
        let stale = InputOwner { tool_start: token(tool.start + 1), ..owner.clone() };
        assert!(ended(&stale).unwrap());
        assert!(process(tool_pid).unwrap().is_some_and(|seen| !seen.ended));
        assert!(ended(&owner).unwrap());
        assert!(process(tool_pid).unwrap().is_none_or(|seen| seen.ended));
    }
}
