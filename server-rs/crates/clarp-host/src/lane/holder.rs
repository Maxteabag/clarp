//! Who holds a lane's owner lock, from the kernel rather than from the lock file's words, and
//! whether a process group is still the lane's tool (issue #772). A lane writes its id into its
//! owner lock only after it took it, so the file can still name the lane before it, whose id
//! may since name another process; `/proc/locks` names the process that holds the lock now.

use std::os::unix::fs::MetadataExt;
use std::path::Path;

/// The process that holds the flock on `lock`, as `/proc/locks` says, if one does.
pub(super) fn of(lock: &Path) -> Option<u32> {
    let file = std::fs::metadata(lock).ok()?;
    let locks = std::fs::read_to_string("/proc/locks").ok()?;
    holding(&locks, file.dev(), file.ino())
}

/// The holder of a flock on the file of device `dev` and inode `ino`, in `/proc/locks` text: a
/// line `N: FLOCK ADVISORY WRITE <pid> <major>:<minor>:<inode> 0 EOF`, the major and minor in
/// hexadecimal. A line of a process waiting for the lock (`->`) holds nothing.
fn holding(locks: &str, dev: u64, ino: u64) -> Option<u32> {
    let wanted = (nix::sys::stat::major(dev), nix::sys::stat::minor(dev), ino);
    locks.lines().find_map(|line| {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.get(1) != Some(&"FLOCK") {
            return None;
        }
        let pid = fields.get(4)?.parse::<u32>().ok()?;
        let mut file = fields.get(5)?.split(':');
        let major = u64::from_str_radix(file.next()?, 16).ok()?;
        let minor = u64::from_str_radix(file.next()?, 16).ok()?;
        let inode = file.next()?.parse::<u64>().ok()?;
        ((major, minor, inode) == wanted).then_some(pid)
    })
}

/// Whether `group` is a process group whose leader is still a child of the lane's process
/// `lane`: the tool the lane started and has not reaped. A leader not yet reaped keeps its id,
/// so it names that tool and nothing else; a group whose leader the lane reaped is left alone,
/// as its id may since name another process.
pub(super) fn leads_for(group: u32, lane: u32) -> bool {
    std::fs::read_to_string(format!("/proc/{group}/stat"))
        .ok()
        .is_some_and(|stat| parent_and_group(&stat) == Some((lane, group)))
}

/// The parent and the group of a process, from its `/proc/<pid>/stat` text: the fields after
/// the command's closing parenthesis are its state, its parent and its group.
fn parent_and_group(stat: &str) -> Option<(u32, u32)> {
    let (_, after) = stat.rsplit_once(')')?;
    let mut fields = after.split_whitespace().skip(1);
    let parent = fields.next()?.parse().ok()?;
    let group = fields.next()?.parse().ok()?;
    Some((parent, group))
}

#[cfg(test)]
mod tests {
    use super::{holding, parent_and_group};

    #[test]
    fn the_holder_of_a_lanes_lock_is_read_from_the_kernels_list() {
        let dev = nix::sys::stat::makedev(0x103, 0x2);
        let locks = "\
1: POSIX  ADVISORY  WRITE 900 103:02:77 0 EOF
2: FLOCK  ADVISORY  WRITE 4242 103:02:131 0 EOF
2: -> FLOCK  ADVISORY  WRITE 5151 103:02:131 0 EOF
3: FLOCK  ADVISORY  WRITE 6262 103:03:131 0 EOF
4: FLOCK  ADVISORY  READ 7373 103:02:999 0 EOF
";
        assert_eq!(holding(locks, dev, 131), Some(4242));
        // Another device's file of the same inode, a POSIX lock, or no line: no holder.
        assert_eq!(
            holding(locks, nix::sys::stat::makedev(0x103, 0x4), 131),
            None
        );
        assert_eq!(holding(locks, dev, 77), None);
        assert_eq!(holding(locks, dev, 5), None);
        assert_eq!(holding("", dev, 131), None);
        // A shared flock is held too.
        assert_eq!(holding(locks, dev, 999), Some(7373));
    }

    #[test]
    fn a_processs_parent_and_group_are_read_past_its_command() {
        // A command may hold spaces and parentheses of its own.
        let stat = "4321 (codex (app) x) S 1234 4321 4321 0 -1 4194560 0 0";
        assert_eq!(parent_and_group(stat), Some((1234, 4321)));
        assert_eq!(parent_and_group("garbage"), None);
    }
}
