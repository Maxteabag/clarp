//! What a lane holds in memory, read from `/proc`: the lane's own process (the shim that keeps
//! the tool) and every process below it, whatever the tool (an app-server, a CLI, a served
//! tool, the MCP servers they started). Proportional set size, so a page shared by two lanes
//! counts half in each and the sum over lanes is what they hold together.

use std::collections::BTreeMap;
use std::path::Path;

/// Bytes held by `pid` and its descendants, or `None` when `pid` cannot be read (gone, or no
/// `/proc`). A descendant that cannot be read counts nothing.
pub(super) fn of(pid: u32) -> Option<u64> {
    of_in(Path::new("/proc"), pid)
}

fn of_in(proc: &Path, pid: u32) -> Option<u64> {
    let own = pss(proc, pid)?;
    let mut children: BTreeMap<u32, Vec<u32>> = BTreeMap::new();
    for entry in std::fs::read_dir(proc).ok()?.flatten() {
        let Some(each) = entry.file_name().to_str().and_then(|n| n.parse::<u32>().ok()) else {
            continue;
        };
        if let Some(parent) = parent(proc, each) {
            children.entry(parent).or_default().push(each);
        }
    }
    let mut total = own;
    let mut below = children.get(&pid).cloned().unwrap_or_default();
    // A tree, so each is met once; the bound only guards a `/proc` that changes under us.
    let mut seen = 0usize;
    while let Some(each) = below.pop() {
        seen += 1;
        if seen > 100_000 {
            break;
        }
        total = total.saturating_add(pss(proc, each).unwrap_or(0));
        if let Some(more) = children.get(&each) {
            below.extend(more);
        }
    }
    Some(total)
}

/// The parent of `pid`, from `stat`: the fourth field, counted after the name's closing
/// parenthesis since a name may hold spaces and parentheses.
fn parent(proc: &Path, pid: u32) -> Option<u32> {
    let stat = std::fs::read_to_string(proc.join(pid.to_string()).join("stat")).ok()?;
    let rest = &stat[stat.rfind(')')? + 1..];
    rest.split_whitespace().nth(1)?.parse().ok()
}

fn pss(proc: &Path, pid: u32) -> Option<u64> {
    let rollup =
        std::fs::read_to_string(proc.join(pid.to_string()).join("smaps_rollup")).ok()?;
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
    use super::of_in;
    use std::path::Path;

    fn process(proc: &Path, pid: u32, parent: u32, pss_kib: u64) {
        let dir = proc.join(pid.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("stat"),
            format!("{pid} (a (b) c) S {parent} {pid} {pid} 0 -1\n"),
        )
        .unwrap();
        std::fs::write(
            dir.join("smaps_rollup"),
            format!("00400000-7fff [rollup]\nRss: {}\nPss: {pss_kib} kB\n", pss_kib * 2),
        )
        .unwrap();
    }

    #[test]
    fn a_lane_holds_what_its_process_and_all_below_it_hold() {
        let proc = std::env::temp_dir().join(format!("lane-memory-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&proc);
        process(&proc, 10, 1, 100); // the lane's shim
        process(&proc, 11, 10, 1000); // its tool
        process(&proc, 12, 11, 10); // an MCP server the tool started
        process(&proc, 20, 1, 5000); // another lane
        std::fs::create_dir_all(proc.join("self")).unwrap();
        assert_eq!(of_in(&proc, 10), Some(1110 * 1024));
        assert_eq!(of_in(&proc, 20), Some(5000 * 1024));
        assert_eq!(of_in(&proc, 99), None);
        std::fs::remove_dir_all(&proc).unwrap();
    }
}
