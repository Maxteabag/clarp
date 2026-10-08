//! What the desktop logs about itself (C++ `StallMonitor` and
//! `MemoryDiagnostics`): moments the GUI thread was blocked, and a minute
//! line of what the process holds, so an out-of-memory kill leaves a trend
//! in the journal.

use std::path::PathBuf;

/// Kernel memory accounting from `/proc/self/status`, in kilobytes. Missing
/// fields are left out (non-Linux, or a sandbox).
pub fn memory_kb(status: &str) -> Vec<(&'static str, i64)> {
    const KEYS: &[&str] = &["VmRSS", "RssAnon", "RssFile", "VmSwap", "VmHWM"];
    let mut values = Vec::new();
    for line in status.lines() {
        for key in KEYS {
            if let Some(rest) = line.strip_prefix(key).and_then(|rest| rest.strip_prefix(':')) {
                values.push((*key, rest.split_whitespace().next().and_then(|v| v.parse().ok()).unwrap_or(0)));
            }
        }
    }
    values
}

/// Resident megabytes from `/proc/self/statm`.
pub fn resident_mb(statm: &str, page_size: i64) -> i64 {
    statm.split_whitespace().nth(1).and_then(|pages| pages.parse::<i64>().ok()).map_or(0, |pages| pages * (page_size / 1024) / 1024)
}

/// User plus system clock ticks from `/proc/self/stat` (the command may
/// contain spaces and parentheses, so fields count from the last `)`).
pub fn cpu_ticks(stat: &str) -> Option<i64> {
    let fields: Vec<&str> = stat.get(stat.rfind(')')? + 2..)?.split(' ').collect();
    Some(fields.get(11)?.parse::<i64>().ok()? + fields.get(12)?.parse::<i64>().ok()?)
}

/// CPU milliseconds per second between two readings; 0 for the first.
#[derive(Debug, Default)]
pub struct CpuRate {
    last: Option<(i64, i64)>,
}

impl CpuRate {
    pub fn sample(&mut self, ticks: i64, now_ms: i64, hz: i64) -> i64 {
        let rate = match self.last {
            Some((last_ticks, last_ms)) if now_ms > last_ms && hz > 0 => (ticks - last_ticks) * 1000 / hz * 1000 / (now_ms - last_ms),
            _ => 0,
        };
        self.last = Some((ticks, now_ms));
        rate
    }
}

/// `$XDG_STATE_HOME/clarp/desktop-stalls.log`, else under `~/.local/state`.
pub fn default_stall_log() -> Option<PathBuf> {
    Some(crate::dirs::state_home()?.join("clarp/desktop-stalls.log"))
}

/// The watchdog's poll interval for a stall threshold.
pub fn poll_interval_ms(threshold_ms: i64) -> i64 {
    (threshold_ms / 2).clamp(75, 500)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StallEvent {
    /// The GUI thread has not beaten for `gap_ms`: capture its stack now.
    Began { gap_ms: i64 },
    /// It beat again after `total_ms` blocked.
    Ended { total_ms: i64 },
}

/// Watches GUI-thread heartbeats (C++ `StallMonitor::watch`). Times are
/// milliseconds on one monotonic clock.
#[derive(Debug)]
pub struct StallDetector {
    threshold_ms: i64,
    stall_start: Option<i64>,
    stalls: u32,
    longest_ms: i64,
}

impl StallDetector {
    pub fn new(threshold_ms: i64) -> Self {
        Self { threshold_ms, stall_start: None, stalls: 0, longest_ms: 0 }
    }

    pub fn stalls(&self) -> u32 {
        self.stalls
    }

    /// The longest stall since the last call, for the minute line.
    pub fn take_longest_ms(&mut self) -> i64 {
        std::mem::take(&mut self.longest_ms)
    }

    /// One watchdog tick: `last_beat` is when the GUI thread last answered
    /// the heartbeat, `last_awake` when its event loop last woke to work.
    /// A block is measured from the wake that started it, so one that begins
    /// just before a beat is not lengthened by the time since the last one.
    pub fn tick(&mut self, last_beat: i64, last_awake: i64, now: i64) -> Option<StallEvent> {
        let start = last_beat.max(last_awake);
        match self.stall_start {
            None if now - start > self.threshold_ms => {
                self.stall_start = Some(start);
                Some(StallEvent::Began { gap_ms: now - start })
            }
            Some(start) if last_beat > start => {
                self.stall_start = None;
                let total_ms = last_beat - start;
                self.stalls += 1;
                self.longest_ms = self.longest_ms.max(total_ms);
                Some(StallEvent::Ended { total_ms })
            }
            _ => None,
        }
    }
}

/// Resident-memory captures: at `first_mb`, then every further 512 MB.
#[derive(Debug)]
pub struct MemoryWatch {
    next_mb: i64,
}

impl MemoryWatch {
    pub fn new(first_mb: i64) -> Self {
        Self { next_mb: first_mb }
    }

    /// True when `resident_mb` crossed the next mark.
    pub fn crossed(&mut self, resident_mb: i64) -> bool {
        if self.next_mb <= 0 || resident_mb < self.next_mb {
            return false;
        }
        self.next_mb = resident_mb + 512;
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proc_files_parse_like_the_kernel_writes_them() {
        let status = "Name:\tclarp\nVmHWM:\t  204800 kB\nVmRSS:\t  102400 kB\nRssAnon:\t   51200 kB\nRssFile:\t   40960 kB\nVmSwap:\t       0 kB\n";
        assert_eq!(memory_kb(status), vec![("VmHWM", 204800), ("VmRSS", 102400), ("RssAnon", 51200), ("RssFile", 40960), ("VmSwap", 0)]);
        assert_eq!(resident_mb("100000 51200 3000 1 0 2000 0", 4096), 200);
        let stat = "4242 (clarp (desk) top) S 1 4242 4242 0 -1 4194560 100 0 0 0 250 75 0 0 20 0 30 0";
        assert_eq!(cpu_ticks(stat), Some(325));
        assert_eq!(cpu_ticks("garbage"), None);
    }

    #[test]
    fn cpu_rate_is_per_second_between_samples() {
        let mut rate = CpuRate::default();
        assert_eq!(rate.sample(100, 10_000, 100), 0);
        // 50 ticks at 100 Hz = 500 ms of CPU over 2 s.
        assert_eq!(rate.sample(150, 12_000, 100), 250);
    }

    #[test]
    fn a_stall_begins_once_and_ends_with_its_length() {
        let mut stalls = StallDetector::new(150);
        assert_eq!(stalls.tick(1_000, 0, 1_100), None);
        assert_eq!(stalls.tick(1_000, 0, 1_200), Some(StallEvent::Began { gap_ms: 200 }));
        assert_eq!(stalls.tick(1_000, 0, 1_400), None, "one capture per stall");
        assert_eq!(stalls.tick(1_900, 1_900, 1_950), Some(StallEvent::Ended { total_ms: 900 }));
        assert_eq!(stalls.tick(1_950, 1_950, 2_000), None);
        assert_eq!(stalls.stalls(), 1);
        assert_eq!(stalls.take_longest_ms(), 900);
        assert_eq!(stalls.take_longest_ms(), 0, "the minute line resets it");
    }

    /// Runs a GUI timeline under the watchdog (C++ `tst_stall_monitor`):
    /// the GUI thread beats every `poll` ms while idle, and at `block_at`
    /// its event loop wakes and blocks for `block_ms`.
    fn run(threshold: i64, block_at: i64, block_ms: i64) -> (u32, i64) {
        let poll = poll_interval_ms(threshold);
        let mut stalls = StallDetector::new(threshold);
        let (mut beat, mut awake) = (0, 0);
        for now in (poll..block_at + block_ms + 1_000).step_by(poll as usize) {
            // Idle, the queued beat is answered at once; blocked, nothing runs.
            let blocked = (block_at..block_at + block_ms).contains(&now);
            if !blocked {
                beat = if now >= block_at + block_ms && beat < block_at + block_ms { block_at + block_ms } else { now };
                awake = beat;
            } else if awake < block_at {
                awake = block_at;
            }
            stalls.tick(beat, awake, now);
        }
        (stalls.stalls(), stalls.take_longest_ms())
    }

    #[test]
    fn short_pauses_are_not_stalls() {
        assert_eq!(run(200, 100, 60).0, 0);
    }

    #[test]
    fn posted_block_durations_use_wake_time() {
        // (delay after a beat, block, stall expected)
        for (delay, block, expect) in [(5, 100, false), (70, 100, false), (5, 250, true), (70, 250, true)] {
            let (count, longest) = run(150, 75 + delay, block);
            assert_eq!(count, u32::from(expect), "delay {delay} block {block}");
            if expect {
                assert!((230..=280).contains(&longest), "delay {delay}: measured {longest} ms");
            }
        }
    }

    #[test]
    fn memory_captures_at_the_mark_and_every_512_mb_more() {
        let mut memory = MemoryWatch::new(1536);
        assert!(!memory.crossed(1500));
        assert!(memory.crossed(1600));
        assert!(!memory.crossed(2000));
        assert!(memory.crossed(2112));
        assert!(!MemoryWatch::new(0).crossed(9999), "0 disables");
        assert_eq!(poll_interval_ms(150), 75);
        assert_eq!(poll_interval_ms(400), 200);
        assert_eq!(poll_interval_ms(5000), 500);
    }
}
