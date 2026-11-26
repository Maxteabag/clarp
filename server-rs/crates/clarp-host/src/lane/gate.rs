//! Who chooses which idle lanes leave when room is made for a lane (issue 908). One run folder,
//! one gate:
//!
//! - A lane some start has chosen to leave is marked until it has been asked, so no other start
//!   counts it as a place of its own, or asks it again: a lane leaving is asked by one, never
//!   twice.
//! - A start for one of the owner's agents chooses first. It says so before it looks at the
//!   lanes; any other start waits, a moment at most, until no start of his is still choosing, so
//!   the idle lanes there are his to take first.
//!
//! The choosing itself is short (a look at what each lane said, no request); the asks to leave
//! are made outside the gate, so nobody waits on another lane's exit to choose.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, OnceLock, PoisonError};
use std::time::{Duration, Instant};

#[derive(Default)]
struct State {
    /// Starts for the owner's agents that have not chosen yet.
    owners: usize,
    /// The sockets of lanes chosen to leave that have not been asked yet.
    leaving: HashSet<PathBuf>,
}

#[derive(Default)]
pub(super) struct Gate {
    state: Mutex<State>,
    changed: Condvar,
}

/// The gate of the lanes of `run`.
pub(super) fn of(run: &Path) -> Arc<Gate> {
    static GATES: OnceLock<Mutex<HashMap<PathBuf, Arc<Gate>>>> = OnceLock::new();
    GATES
        .get_or_init(Mutex::default)
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .entry(run.to_owned())
        .or_default()
        .clone()
}

impl Gate {
    fn state(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// A start for one of the owner's agents, said before it looks at the lanes: until it is
    /// dropped (once it has chosen), the starts of others wait for it.
    pub(super) fn owner_first(self: &Arc<Self>) -> OwnerFirst {
        self.state().owners += 1;
        OwnerFirst(self.clone())
    }

    /// Any other start: waits, at most `bound`, until no start of the owner's is still choosing.
    /// Whether it did not have to wait out the bound.
    pub(super) fn after_owners(&self, bound: Duration) -> bool {
        let until = Instant::now() + bound;
        let mut state = self.state();
        while state.owners > 0 {
            let left = until.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return false;
            }
            state = self
                .changed
                .wait_timeout(state, left)
                .unwrap_or_else(PoisonError::into_inner)
                .0;
        }
        true
    }

    /// Chooses under the gate: `choose` is given the lanes already chosen to leave by others, and
    /// says what it decided and the sockets of the lanes it chose, which are marked as leaving
    /// until [`Self::asked`].
    pub(super) fn choose<T>(
        &self,
        choose: impl FnOnce(&HashSet<PathBuf>) -> (T, Vec<PathBuf>),
    ) -> T {
        let mut state = self.state();
        let (decided, chosen) = choose(&state.leaving);
        state.leaving.extend(chosen);
        decided
    }

    /// The lanes of `sockets` have been asked to leave, and have gone or refused: unmarked.
    pub(super) fn asked<'a>(&self, sockets: impl IntoIterator<Item = &'a Path>) {
        let mut state = self.state();
        for socket in sockets {
            state.leaving.remove(socket);
        }
    }
}

/// A start of the owner's that has not chosen yet.
pub(super) struct OwnerFirst(Arc<Gate>);

impl Drop for OwnerFirst {
    fn drop(&mut self) {
        let mut state = self.0.state();
        state.owners = state.owners.saturating_sub(1);
        drop(state);
        self.0.changed.notify_all();
    }
}
