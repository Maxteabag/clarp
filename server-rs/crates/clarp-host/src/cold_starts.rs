//! The turns that started their tool anew, without a lane, in the last hour
//! (`cold_starts_last_hour` in `GET /lane-settings`): what loads a machine is the tools started
//! anew for each turn, not the processes that live (the owner's word of 25 Nov 2026).

use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex, OnceLock, PoisonError};
use std::time::Duration;

use clarp_ports::clock::Clock;

/// How far back the turns that started without a lane are counted.
const WITHIN: Duration = Duration::from_secs(60 * 60);
/// The most cold starts kept: an hour of them on a busy Host, with room to spare.
const KEPT: usize = 10_000;

/// The cold starts of the last hour.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct Counted {
    pub total: usize,
    /// By the agent's tool (its backend).
    pub tools: BTreeMap<String, usize>,
}

/// When each turn that started without a lane started, on the Host's clock, and its tool.
pub struct ColdStarts {
    clock: Arc<dyn Clock + Send + Sync>,
    started: Mutex<VecDeque<(i64, String)>>,
}

impl ColdStarts {
    pub fn new(clock: Arc<dyn Clock + Send + Sync>) -> Arc<Self> {
        Arc::new(Self {
            clock,
            started: Mutex::default(),
        })
    }

    /// A turn of an agent of `tool` starts without a lane: its tool is started anew.
    pub(crate) fn started(&self, tool: &str) {
        let now = self.clock.now_ms();
        let mut started = self.started.lock().unwrap_or_else(PoisonError::into_inner);
        forget_old(&mut started, now);
        if started.len() >= KEPT {
            started.pop_front();
        }
        started.push_back((now, tool.to_owned()));
    }

    /// The turns that started without a lane in the last hour.
    pub(crate) fn counted(&self) -> Counted {
        let mut started = self.started.lock().unwrap_or_else(PoisonError::into_inner);
        forget_old(&mut started, self.clock.now_ms());
        let mut counted = Counted {
            total: started.len(),
            tools: BTreeMap::new(),
        };
        for (_, tool) in started.iter() {
            *counted.tools.entry(tool.clone()).or_default() += 1;
        }
        counted
    }
}

fn forget_old(started: &mut VecDeque<(i64, String)>, now: i64) {
    let within = i64::try_from(WITHIN.as_millis()).unwrap_or(i64::MAX);
    while started
        .front()
        .is_some_and(|(at, _)| now.saturating_sub(*at) >= within)
    {
        started.pop_front();
    }
}

/// The count of this Host's process, for what is given no handle on it (the agents the fleet
/// opens).
static SHARED: OnceLock<Arc<ColdStarts>> = OnceLock::new();

/// Makes `cold` the count of this process. Once, at the Host's start.
pub fn install(cold: Arc<ColdStarts>) {
    if SHARED.set(cold).is_err() {
        eprintln!("clarp-host: the count of cold starts was set already");
    }
}

/// The count of this process; where none was installed (a unit test), one of its own.
pub(crate) fn shared() -> Arc<ColdStarts> {
    static OWN: OnceLock<Arc<ColdStarts>> = OnceLock::new();
    SHARED
        .get()
        .unwrap_or_else(|| {
            OWN.get_or_init(|| ColdStarts::new(Arc::new(clarp_ports::clock::SystemClock)))
        })
        .clone()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]

    use std::sync::atomic::{AtomicI64, Ordering};

    use super::*;

    /// A clock a test moves by hand.
    #[derive(Default)]
    struct Hand(AtomicI64);

    impl Clock for Hand {
        fn now_ms(&self) -> i64 {
            self.0.load(Ordering::SeqCst)
        }
    }

    #[test]
    fn the_cold_starts_of_the_last_hour_are_counted_by_tool() {
        let clock = Arc::new(Hand::default());
        let cold = ColdStarts::new(clock.clone());
        assert_eq!(cold.counted(), Counted::default());
        cold.started("claude");
        clock.0.store(30 * 60_000, Ordering::SeqCst);
        cold.started("codex");
        cold.started("claude");
        let counted = cold.counted();
        assert_eq!(counted.total, 3);
        assert_eq!(
            counted.tools,
            BTreeMap::from([("claude".to_owned(), 2), ("codex".to_owned(), 1)])
        );
        // An hour after the first, it is no longer counted.
        clock.0.store(60 * 60_000, Ordering::SeqCst);
        let counted = cold.counted();
        assert_eq!(counted.total, 2);
        assert_eq!(
            counted.tools,
            BTreeMap::from([("claude".to_owned(), 1), ("codex".to_owned(), 1)])
        );
        clock.0.store(91 * 60_000, Ordering::SeqCst);
        assert_eq!(cold.counted(), Counted::default());
    }
}
