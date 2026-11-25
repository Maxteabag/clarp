//! A lane's own systemd scope, by which a lane that does not let go is ended with its tool
//! (issue #772). The launcher starts every lane in a scope of its own
//! (`clarp-claude-lane-*.scope`, `clarp-codex-goal-*.scope`). A scope is a cgroup: ending the
//! unit ends the lane and every process it started, whatever their process groups, and a unit's
//! name is never reused while it runs, so it names that lane and nothing else. The unit is read
//! from the lane's process (`/proc/<pid>/cgroup`), so it is known after a Host restart too, when
//! the Host no longer knows the name it launched.

use std::path::Path;
use std::process::Stdio;
use std::time::{Duration, Instant};

/// How long `systemctl` is given to end a scope.
const SYSTEMCTL_WAIT: Duration = Duration::from_secs(5);
/// How often its end is looked for.
const SYSTEMCTL_LOOK: Duration = Duration::from_millis(20);

/// The lane scope the process `pid` runs in, if it runs in one of the Host's lane scopes that
/// is not the unit this Host runs in itself.
pub(super) fn of_process(pid: u32) -> Option<String> {
    let read = |path: &Path| std::fs::read_to_string(path).ok();
    let lane = read(&Path::new("/proc").join(pid.to_string()).join("cgroup"))?;
    let own = read(Path::new("/proc/self/cgroup")).unwrap_or_default();
    of(&lane, &own)
}

/// The lane scope in a process's cgroup text `cgroup`, where its unit is one of the Host's
/// lane scopes and not the unit of `own` (the Host's own cgroup text). A lane started outside
/// systemd shares its starter's unit, which is no lane scope, or is the unit the Host runs in:
/// that is never ended, as it would end the Host or whoever started it.
pub(super) fn of(cgroup: &str, own: &str) -> Option<String> {
    let named = unit(cgroup)?;
    (is_lane_scope(named) && unit(own) != Some(named)).then(|| named.to_owned())
}

/// The unit a cgroup text names: the last part of the unified hierarchy's path (`0::/...`),
/// or of the systemd hierarchy's (`N:name=systemd:/...`) on a host that has no unified one.
fn unit(cgroup: &str) -> Option<&str> {
    let path_of = |wanted: fn(&str, &str) -> bool| {
        cgroup.lines().find_map(|line| {
            let mut fields = line.splitn(3, ':');
            let (id, controllers, path) = (fields.next()?, fields.next()?, fields.next()?);
            wanted(id, controllers).then_some(path)
        })
    };
    let path = path_of(|id, controllers| id == "0" && controllers.is_empty())
        .or_else(|| path_of(|_, controllers| controllers == "name=systemd"))?;
    path.rsplit('/').next().filter(|unit| !unit.is_empty())
}

/// Whether `unit` is a scope the launcher starts a lane in: `clarp-<tool>-lane-*.scope`, or
/// the Codex goal lane's `clarp-codex-goal-*.scope`. A turn's scope (`clarp-turn-*`) is not.
fn is_lane_scope(unit: &str) -> bool {
    unit.strip_suffix(".scope")
        .and_then(|name| name.strip_prefix("clarp-"))
        .is_some_and(|name| name.starts_with("codex-goal-") || name.contains("-lane-"))
}

/// Ends every process of the scope `unit` with SIGKILL (`systemctl --user kill`), waiting at
/// most [`SYSTEMCTL_WAIT`] for `systemctl`. True: it said it did.
pub(super) fn end(unit: &str) -> bool {
    let Ok(mut systemctl) = crate::child::command("systemctl")
        .args(["--user", "kill", "--signal=SIGKILL", "--"])
        .arg(unit)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    else {
        return false;
    };
    let until = Instant::now() + SYSTEMCTL_WAIT;
    loop {
        match systemctl.try_wait() {
            Ok(Some(status)) => return status.success(),
            Ok(None) if Instant::now() < until => std::thread::sleep(SYSTEMCTL_LOOK),
            _ => {
                let _ = systemctl.kill();
                let _ = systemctl.wait();
                return false;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::of;

    const HOST: &str = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/clarp.service\n";

    #[test]
    fn a_lanes_scope_is_read_from_its_cgroup_and_only_a_lanes_scope_is_named() {
        let unified = |unit: &str| {
            format!("0::/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}\n")
        };
        for lane in [
            "clarp-claude-lane-1a2b.scope",
            "clarp-codex-goal-3c4d.scope",
            "clarp-muse-lane-5e6f.scope",
        ] {
            assert_eq!(of(&unified(lane), HOST).as_deref(), Some(lane), "{lane}");
        }
        // A turn's scope, a service, another program's scope, or none of the Host's: no lane.
        for other in [
            "clarp-turn-12-1a2b.scope",
            "clarp.service",
            "app-kitty-1234.scope",
            "lane-1a2b.scope",
            "clarp-claude-lane-1a2b.service",
            "",
        ] {
            assert_eq!(of(&unified(other), HOST), None, "{other:?}");
        }
        // A host with only the systemd hierarchy, beside the controllers' own lines.
        let hybrid = "12:memory:/user.slice\n1:name=systemd:/user.slice/user-1000.slice/\
                      user@1000.service/app.slice/clarp-claude-lane-9f8e.scope\n";
        assert_eq!(
            of(hybrid, HOST).as_deref(),
            Some("clarp-claude-lane-9f8e.scope")
        );
        // Unreadable or empty text names nothing.
        assert_eq!(of("", HOST), None);
        assert_eq!(of("garbage", HOST), None);
    }

    #[test]
    fn the_unit_the_host_runs_in_is_never_a_lane_to_end() {
        // A lane started outside systemd runs in its starter's unit; when that is the Host's
        // own (a Host or a test run inside a lane's scope), ending it would end the Host.
        let shared = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/\
                      clarp-claude-lane-1a2b.scope\n";
        assert_eq!(of(shared, shared), None);
        assert_eq!(
            of(shared, HOST).as_deref(),
            Some("clarp-claude-lane-1a2b.scope")
        );
    }
}
