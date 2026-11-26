//! Who chooses which idle lanes leave when room is made for a lane (issue 908). One run folder,
//! one gate, held only while a start chooses (a look at what each lane said, no request); the
//! asks to leave are made outside it, and nobody waits there for anything:
//!
//! - A lane some start has chosen to leave is marked, by its socket as it was when chosen, so no
//!   other start chooses it or counts it as a place of its own. A lane that did not leave (it
//!   refused, or said nothing) is unmarked once asked; one that left stays marked until its
//!   socket is gone or another lane's has taken its name. A lane that leaves is counted by one
//!   start, never two.
//! - A start for one of the owner's agents is counted at the gate, with what its lane counts at,
//!   before it looks at the lanes. A start of the agents' own that chooses while his is still
//!   looking leaves room for his as if his lane were already there, so where one place can be
//!   made, it is his. A start of the agents' own that chose before his reached the gate keeps
//!   what it chose.

use std::collections::{HashMap, HashSet};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, PoisonError};

/// Which socket file a lane listens on: a lane that leaves and one that later takes its name
/// are told apart.
fn identity(socket: &Path) -> Option<(u64, u64)> {
    std::fs::symlink_metadata(socket)
        .ok()
        .map(|meta| (meta.dev(), meta.ino()))
}

/// The starts for the owner's agents that are counted at the gate and have not chosen yet.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub(super) struct Ahead {
    /// How many.
    pub count: usize,
    /// What their lanes count at together.
    pub bytes: u64,
}

#[derive(Default)]
struct State {
    ahead: Ahead,
    /// The lanes chosen to leave, by their socket as it was when chosen.
    leaving: HashMap<PathBuf, (u64, u64)>,
}

/// What a start sees when it chooses.
pub(super) struct Seen<'a> {
    leaving: &'a HashMap<PathBuf, (u64, u64)>,
    /// The owner's starts it leaves room for: none for a start of his own.
    pub ahead: Ahead,
}

impl Seen<'_> {
    /// Whether the lane of `socket` may still be chosen: it is there, and no start chose it.
    pub(super) fn free(&self, socket: &Path) -> bool {
        identity(socket).is_some_and(|id| self.leaving.get(socket) != Some(&id))
    }
}

#[derive(Default)]
pub(super) struct Gate {
    state: Mutex<State>,
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

    /// A start for one of the owner's agents, whose lane counts at `bytes`, counted before it
    /// looks at the lanes, until it chooses or is dropped.
    pub(super) fn owner_first(self: &Arc<Self>, bytes: u64) -> OwnerFirst {
        let mut state = self.state();
        state.ahead.count += 1;
        state.ahead.bytes = state.ahead.bytes.saturating_add(bytes);
        OwnerFirst {
            gate: self.clone(),
            bytes,
            counted: true,
        }
    }

    /// Chooses under the gate. `first` is the start's own count when it is the owner's: it stops
    /// counting in the same moment, so no other start sees it both as chosen and as ahead.
    /// `choose` is given what the start sees, and says what it decided and the sockets of the
    /// lanes it chose, which are marked as leaving.
    pub(super) fn choose<T>(
        &self,
        first: Option<OwnerFirst>,
        choose: impl FnOnce(&Seen<'_>) -> (T, Vec<PathBuf>),
    ) -> T {
        let mut state = self.state();
        state
            .leaving
            .retain(|socket, id| identity(socket) == Some(*id));
        let ahead = match first {
            Some(mut first) => {
                state.ahead.count = state.ahead.count.saturating_sub(1);
                state.ahead.bytes = state.ahead.bytes.saturating_sub(first.bytes);
                first.counted = false;
                Ahead::default()
            }
            None => state.ahead,
        };
        let seen = Seen {
            leaving: &state.leaving,
            ahead,
        };
        let (decided, chosen) = choose(&seen);
        for socket in chosen {
            if let Some(id) = identity(&socket) {
                state.leaving.insert(socket, id);
            }
        }
        decided
    }

    /// The lanes chosen have been asked to leave: each with whether it left. One that did not is
    /// unmarked, and may be chosen again; one that left stays marked until its socket has gone.
    pub(super) fn asked<'a>(&self, asked: impl IntoIterator<Item = (&'a Path, bool)>) {
        let mut state = self.state();
        let stayed: HashSet<&Path> = asked
            .into_iter()
            .filter(|(_, left)| !left)
            .map(|(socket, _)| socket)
            .collect();
        state
            .leaving
            .retain(|socket, _| !stayed.contains(socket.as_path()));
    }
}

/// A start of the owner's, counted at the gate until it chooses.
pub(super) struct OwnerFirst {
    gate: Arc<Gate>,
    bytes: u64,
    counted: bool,
}

impl Drop for OwnerFirst {
    fn drop(&mut self) {
        if self.counted {
            let mut state = self.gate.state();
            state.ahead.count = state.ahead.count.saturating_sub(1);
            state.ahead.bytes = state.ahead.bytes.saturating_sub(self.bytes);
        }
    }
}
