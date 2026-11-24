//! What a lane holds in memory, read by the lane itself from `/proc`: its own process, every
//! process below it, and every process of its tool's group (an MCP server the tool started stays
//! in that group even where its parent left it). Proportional set size, so a page two lanes share
//! counts half in each, and the Host's sum over lanes is what they hold together (issue 761).

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

/// Bytes held by the lane process `own` and the tool group `group`, or `None` where `own` cannot
/// be read (no `/proc`). A process that cannot be read counts nothing.
pub(super) fn held(own: u32, group: u32) -> Option<u64> {
    held_in(Path::new("/proc"), own, group)
}

fn held_in(proc: &Path, own: u32, group: u32) -> Option<u64> {
    pss(proc, own)?;
    let mut children: BTreeMap<u32, Vec<u32>> = BTreeMap::new();
    let mut members = BTreeSet::from([own]);
    for entry in std::fs::read_dir(proc).ok()?.flatten() {
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<u32>().ok())
        else {
            continue;
        };
        let Some((parent, pgid)) = stat(proc, pid) else {
            continue;
        };
        children.entry(parent).or_default().push(pid);
        if pgid == group {
            members.insert(pid);
        }
    }
    let mut below = vec![own];
    while let Some(pid) = below.pop() {
        for child in children.get(&pid).into_iter().flatten() {
            if members.insert(*child) {
                below.push(*child);
            }
        }
    }
    Some(
        members
            .iter()
            .filter_map(|pid| pss(proc, *pid))
            .fold(0u64, u64::saturating_add),
    )
}

/// A process's parent and group, from `stat`: the fields after the name's closing parenthesis,
/// since a name may hold spaces and parentheses.
fn stat(proc: &Path, pid: u32) -> Option<(u32, u32)> {
    let stat = std::fs::read_to_string(proc.join(pid.to_string()).join("stat")).ok()?;
    let mut fields = stat[stat.rfind(')')? + 1..].split_whitespace().skip(1);
    let parent = fields.next()?.parse().ok()?;
    let group = fields.next()?.parse().ok()?;
    Some((parent, group))
}

fn pss(proc: &Path, pid: u32) -> Option<u64> {
    let rollup = std::fs::read_to_string(proc.join(pid.to_string()).join("smaps_rollup")).ok()?;
    let kib = rollup
        .lines()
        .find_map(|line| line.strip_prefix("Pss:"))?
        .split_whitespace()
        .next()?
        .parse::<u64>()
        .ok()?;
    Some(kib.saturating_mul(1024))
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::{held, held_in};
    use std::path::Path;

    fn process(proc: &Path, pid: u32, parent: u32, group: u32, pss_kib: u64) {
        let dir = proc.join(pid.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("stat"),
            format!("{pid} (a (b) c) S {parent} {group} {group} 0 -1\n"),
        )
        .unwrap();
        std::fs::write(
            dir.join("smaps_rollup"),
            format!(
                "00400000-7fff [rollup]\nRss: {}\nPss: {pss_kib} kB\n",
                pss_kib * 2
            ),
        )
        .unwrap();
    }

    #[test]
    fn a_lane_holds_itself_what_is_below_it_and_its_tools_group() {
        let proc = std::env::temp_dir().join(format!("lane-memory-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&proc);
        process(&proc, 10, 1, 10, 5); // the lane process
        process(&proc, 11, 10, 11, 200); // its app-server, leading a group of its own
        process(&proc, 12, 11, 11, 30); // an MCP server it started
        process(&proc, 13, 1, 11, 7); // one of its group whose parent left it
        process(&proc, 20, 1, 20, 5000); // another lane
        std::fs::create_dir_all(proc.join("self")).unwrap();
        assert_eq!(held_in(&proc, 10, 11), Some(242 * 1024));
        assert_eq!(held_in(&proc, 99, 11), None);
        std::fs::remove_dir_all(&proc).unwrap();
    }

    #[test]
    fn this_process_holds_something() {
        let me = std::process::id();
        assert!(held(me, me).is_some_and(|bytes| bytes > 0));
    }
}
