//! The Host's open files. Each agent keeps its log database open with its two companions
//! (`-wal`, `-shm`), about three descriptors an agent, so a Host with a few hundred agents
//! needs more than the 1,024 a container is given by default. At start the Host raises its
//! own soft limit to the hard one, and it says once when it passes 80% of it.

use nix::sys::resource::{Resource, getrlimit, setrlimit};

/// The soft limit to set, when the soft limit is below the hard one: the hard limit.
fn raised(soft: u64, hard: u64) -> Option<u64> {
    (soft < hard).then_some(hard)
}

/// Whether `held` descriptors are 80% of the soft limit or more.
fn past_watermark(held: u64, soft: u64) -> bool {
    held.saturating_mul(5) >= soft.saturating_mul(4)
}

/// Raises the soft open-files limit to the hard limit and says so. A limit that cannot be
/// read or raised is said, and the Host starts all the same.
pub fn raise() {
    let (soft, hard) = match getrlimit(Resource::RLIMIT_NOFILE) {
        Ok(limits) => limits,
        Err(why) => {
            eprintln!("clarp-host: the open-files limit cannot be read: {why}");
            return;
        }
    };
    let Some(wanted) = raised(soft, hard) else {
        eprintln!("clarp-host: open files: soft limit {soft}, hard limit {hard}, kept");
        return;
    };
    match setrlimit(Resource::RLIMIT_NOFILE, wanted, hard) {
        Ok(()) => eprintln!(
            "clarp-host: open files: soft limit {soft} raised to {wanted} (hard limit {hard})"
        ),
        Err(why) => eprintln!(
            "clarp-host: open files: soft limit {soft} could not be raised to {wanted} (hard limit {hard}): {why}"
        ),
    }
}

/// Says once in the life of the process when it holds 80% of its soft open-files limit.
#[derive(Default)]
pub struct Watermark {
    told: bool,
}

impl Watermark {
    /// Counts the descriptors held now and says it the first time they pass 80% of the soft
    /// limit, with the number of agents.
    pub fn check(&mut self, agents: usize) {
        if self.told {
            return;
        }
        let Ok(fds) = std::fs::read_dir("/proc/self/fd") else {
            return;
        };
        let Ok((soft, _)) = getrlimit(Resource::RLIMIT_NOFILE) else {
            return;
        };
        let held = fds.count() as u64;
        if past_watermark(held, soft) {
            self.told = true;
            eprintln!(
                "clarp-host: open files: {held} descriptors held of a soft limit of {soft}, with {agents} agents"
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_soft_limit_below_the_hard_one_is_raised_to_it() {
        assert_eq!(raised(1_024, 1_048_576), Some(1_048_576));
        assert_eq!(raised(1_024, 4_096), Some(4_096));
    }

    #[test]
    fn a_soft_limit_equal_to_the_hard_one_is_kept() {
        assert_eq!(raised(65_536, 65_536), None);
        assert_eq!(raised(u64::MAX, u64::MAX), None);
    }

    #[test]
    fn the_watermark_is_eighty_percent_of_the_soft_limit() {
        assert!(!past_watermark(818, 1_024));
        assert!(past_watermark(820, 1_024));
        assert!(past_watermark(1_024, 1_024));
        assert!(!past_watermark(1_178, 65_536));
        assert!(!past_watermark(1_000, u64::MAX));
    }
}
