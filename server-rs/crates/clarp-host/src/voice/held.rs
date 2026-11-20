//! The agents whose lines wait for their call: those whose opening binding was not recovered
//! when the Host started, because the agent did not answer then.
//!
//! An agent's actor opens again by itself, and from then on its lines can be claimed while its
//! binding was never recovered: a greeting meant for a call would be said as an ordinary clip,
//! which is what the gate before the one that speaks exists to prevent. So such an agent is
//! held: a line it hands over is not said until its binding is recovered, and the recovery is
//! tried at that moment, before the line is judged. Recovered, the agent is let go and the line
//! is judged as any line; not recovered, the line is not said. No timer.

use std::collections::BTreeSet;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex, OnceLock};

/// What recovers an agent's opening bindings: answers whether every one of them is recovered.
pub trait Recover: Send + Sync {
    fn recover(&self, agent_id: String) -> Pin<Box<dyn Future<Output = bool> + Send + '_>>;
}

/// The agents held, and what recovers them once the Host can.
#[derive(Default)]
pub struct Held {
    agents: Mutex<BTreeSet<String>>,
    recover: OnceLock<Arc<dyn Recover>>,
}

impl Held {
    /// The agent's lines wait for its binding from now on.
    pub fn hold(&self, agent_id: &str) {
        if let Ok(mut agents) = self.agents.lock() {
            agents.insert(agent_id.to_string());
        }
    }

    /// The agent's binding is recovered: its lines are judged as any line.
    pub fn release(&self, agent_id: &str) {
        if let Ok(mut agents) = self.agents.lock() {
            agents.remove(agent_id);
        }
    }

    /// Whether the agent is held.
    pub fn holds(&self, agent_id: &str) -> bool {
        self.agents
            .lock()
            .is_ok_and(|agents| agents.contains(agent_id))
    }

    /// What recovers an agent's bindings: given once, when the Host can recover them.
    pub fn recovered_by(&self, recover: Arc<dyn Recover>) {
        self.recover.get_or_init(|| recover);
    }

    /// Whether a line of this agent may be judged now: always where it is not held; where it is,
    /// only once its binding was recovered, which is tried now. A recovery that succeeds lets
    /// the agent go.
    pub async fn may_speak(&self, agent_id: &str) -> bool {
        if !self.holds(agent_id) {
            return true;
        }
        let Some(recover) = self.recover.get() else {
            return false;
        };
        if recover.recover(agent_id.to_string()).await {
            self.release(agent_id);
            return true;
        }
        false
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::future::Future;
    use std::pin::Pin;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

    use super::{Held, Recover};

    /// A recovery that succeeds or not, and counts how often it was tried.
    struct Stand {
        works: AtomicBool,
        tried: AtomicU32,
    }

    impl Recover for Stand {
        fn recover(&self, _: String) -> Pin<Box<dyn Future<Output = bool> + Send + '_>> {
            self.tried.fetch_add(1, Ordering::SeqCst);
            let works = self.works.load(Ordering::SeqCst);
            Box::pin(async move { works })
        }
    }

    #[tokio::test]
    async fn a_held_agent_speaks_once_its_binding_is_recovered_and_not_before() {
        let held = Held::default();
        // Nobody held: every line is judged, nothing is tried.
        assert!(held.may_speak("a-1").await);
        // Held before the Host can recover anything: not said.
        held.hold("a-1");
        assert!(!held.may_speak("a-1").await);
        let stand = Arc::new(Stand {
            works: AtomicBool::new(false),
            tried: AtomicU32::new(0),
        });
        held.recovered_by(stand.clone());
        // Its binding still cannot be recovered: tried with each line, and not said.
        assert!(!held.may_speak("a-1").await);
        assert!(!held.may_speak("a-1").await);
        assert_eq!(stand.tried.load(Ordering::SeqCst), 2);
        assert!(held.holds("a-1"));
        // Another agent is not held by it.
        assert!(held.may_speak("a-2").await);
        assert_eq!(stand.tried.load(Ordering::SeqCst), 2);
        // Recovered at last: said, and let go; its next line is not tried again.
        stand.works.store(true, Ordering::SeqCst);
        assert!(held.may_speak("a-1").await);
        assert!(!held.holds("a-1"));
        assert!(held.may_speak("a-1").await);
        assert_eq!(stand.tried.load(Ordering::SeqCst), 3);
    }
}
