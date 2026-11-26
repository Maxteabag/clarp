//! Who chooses which idle lanes leave when room is made for a lane, and what each start counts
//! (issue 908). One run folder, one gate, held only while a start chooses or settles (a look at
//! what each lane said and at what the gate keeps, no request); the asks to leave are made
//! outside it, and nobody waits there for anything:
//!
//! - Each start is counted at the gate when it begins to look at the lanes ([`Gate::look`]). What
//!   it saw is reconciled with what happened since, under the gate, both when it chooses and when
//!   it settles: a lane it counted that has left for any start no longer counts, and a lane
//!   another start was let start that its look does not show counts, until that lane listens
//!   where every later look sees it, or its launch failed. Two starts never take one place.
//! - A lane some start has chosen to leave is marked by the socket instance that start looked at,
//!   so no other start chooses it. Being chosen is no exit: the lane counts as it was. One that
//!   did not leave (it refused, or said nothing) is unmarked once asked; one that left stays
//!   marked until no look that may have counted it is still open. A replacement lane that took
//!   the same name is another instance, and no ask or answer about the old one touches its mark.
//! - A start for one of the owner's agents is counted at the gate, with what its lane counts at,
//!   from the moment it begins to look. A start of the agents' own that chooses while his has not
//!   leaves room for his as if his lane were already there, so where one place can be made, it is
//!   his. A start of the agents' own that chose before his reached the gate keeps what it chose.

use std::collections::{BTreeSet, HashMap};
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock, PoisonError};

/// Which socket file a lane listens on, by its device and inode: a lane that leaves and one that
/// later takes its name are told apart.
pub(super) type Id = (u64, u64);

pub(super) fn identity(socket: &Path) -> Option<Id> {
    std::fs::symlink_metadata(socket)
        .ok()
        .map(|meta| (meta.dev(), meta.ino()))
}

/// The process that listens on the other end of a connection to a lane.
fn peer(stream: &UnixStream) -> Option<i32> {
    rustix::net::sockopt::socket_peercred(stream)
        .ok()
        .map(|cred| cred.pid.as_raw_nonzero().get())
}

/// The lane one look reached: its socket file, the same before and after the connection was
/// made, and the process that listened on it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct Instance {
    pub id: Id,
    pid: Option<i32>,
}

impl Instance {
    /// The instance `stream` reached, `before` being the socket file as it was just before the
    /// connection was made. `None` where the file changed meanwhile: which lane answered is not
    /// known.
    pub(super) fn reached(socket: &Path, before: Option<Id>, stream: &UnixStream) -> Option<Self> {
        let id = before?;
        (identity(socket) == Some(id)).then(|| Self {
            id,
            pid: peer(stream),
        })
    }

    /// Whether its socket file is still the one this instance listens on.
    pub(super) fn is_at(&self, socket: &Path) -> bool {
        identity(socket) == Some(self.id)
    }

    /// Whether `stream`, just connected to `socket`, reached this instance and no other: the file
    /// is still its own after the connection was made, and the process on the other end is the
    /// one that answered the look.
    pub(super) fn is_reached_by(&self, socket: &Path, stream: &UnixStream) -> bool {
        self.is_at(socket) && peer(stream) == self.pid
    }
}

/// A lane as a start counted it: where it listens, which instance answered (`None` where that
/// is not known), and what it counts at.
#[derive(Debug, Clone)]
pub(super) struct Counted {
    pub socket: PathBuf,
    pub id: Option<Id>,
    pub bytes: u64,
}

/// Lanes still to come that a start leaves room for: how many, and what they count at together.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub(super) struct Coming {
    pub count: usize,
    pub bytes: u64,
}

impl Coming {
    pub(super) fn and(self, other: Self) -> Self {
        Self {
            count: self.count + other.count,
            bytes: self.bytes.saturating_add(other.bytes),
        }
    }
}

/// What happened since a start looked, as it bears on what that look counted.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub(super) struct Since {
    /// Lanes the look counted that have left for some start since, and what they counted at.
    pub left: usize,
    pub freed: u64,
    /// Lanes other starts were let start that the look does not show.
    pub starting: Coming,
}

/// A lane chosen to leave, by the instance its start looked at.
struct Mark {
    socket: PathBuf,
    /// It said it goes, or its socket went after it was asked.
    left: bool,
    /// When its socket was first seen gone or taken by another lane, on the gate's count.
    gone_at: Option<u64>,
}

/// A lane a start was let start, until every look sees it.
struct Admitted {
    socket: PathBuf,
    bytes: u64,
    /// When it was known to listen, on the gate's count.
    visible_at: Option<u64>,
}

#[derive(Default)]
struct State {
    /// The gate's own count of what happens at it: looks begun, sockets seen gone, lanes seen
    /// listening.
    at: u64,
    /// The looks still open, by when they began.
    looks: BTreeSet<u64>,
    /// The owner's starts that have not chosen yet, by their look: what their lanes count at.
    ahead: HashMap<u64, u64>,
    marks: HashMap<Id, Mark>,
    /// By the look that admitted it.
    admitted: HashMap<u64, Admitted>,
}

impl State {
    fn next(&mut self) -> u64 {
        self.at += 1;
        self.at
    }

    /// Forgets what no open look can still count.
    fn prune(&mut self) {
        let now = self.at;
        let oldest = self.looks.first().copied();
        let none_before = |at: u64| oldest.is_none_or(|oldest| oldest > at);
        self.marks.retain(|id, mark| {
            if identity(&mark.socket) == Some(*id) {
                return true;
            }
            // Chosen and not left, and gone by itself or taken by another lane: its start's ask
            // finds no such lane, and nothing counts it as gone.
            if !mark.left {
                return false;
            }
            !none_before(*mark.gone_at.get_or_insert(now))
        });
        self.admitted
            .retain(|_, admitted| admitted.visible_at.is_none_or(|at| !none_before(at)));
    }

    fn since(&self, look: u64, lanes: &[Counted]) -> Since {
        let mut since = Since::default();
        for lane in lanes {
            if lane
                .id
                .and_then(|id| self.marks.get(&id))
                .is_some_and(|mark| mark.left)
            {
                since.left += 1;
                since.freed = since.freed.saturating_add(lane.bytes);
            }
        }
        for (by, admitted) in &self.admitted {
            // Its own, or one the look shows, or one that listened before the look began and is
            // not among what it saw: it has left again.
            if *by == look
                || lanes.iter().any(|lane| lane.socket == admitted.socket)
                || admitted.visible_at.is_some_and(|at| at < look)
            {
                continue;
            }
            since.starting = since.starting.and(Coming {
                count: 1,
                bytes: admitted.bytes,
            });
        }
        since
    }

    fn ahead_of<'a>(&self, starts: impl IntoIterator<Item = &'a u64>) -> Coming {
        starts
            .into_iter()
            .filter_map(|start| self.ahead.get(start))
            .fold(Coming::default(), |coming, bytes| {
                coming.and(Coming {
                    count: 1,
                    bytes: *bytes,
                })
            })
    }
}

/// What a start sees when it chooses.
pub(super) struct Seen<'a> {
    marks: &'a HashMap<Id, Mark>,
    /// The owner's starts it leaves room for: none for a start of his own.
    pub ahead: Coming,
    pub since: Since,
}

impl Seen<'_> {
    /// Whether the lane of `socket`, as the look reached it, may still be chosen: that instance
    /// is still there, and no start chose it.
    pub(super) fn free(&self, socket: &Path, seen: Instance) -> bool {
        seen.is_at(socket) && !self.marks.contains_key(&seen.id)
    }

    /// Every lane still to come that the start leaves room for.
    pub(super) fn coming(&self) -> Coming {
        self.ahead.and(self.since.starting)
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

    /// A start that is about to look at the lanes, counted from now until it is dropped. A start
    /// for one of the owner's agents gives what its lane counts at (`owners`): it is ahead of
    /// the agents' own until it chooses.
    pub(super) fn look(self: &Arc<Self>, owners: Option<u64>) -> Look {
        let mut state = self.state();
        let at = state.next();
        state.looks.insert(at);
        if let Some(bytes) = owners {
            state.ahead.insert(at, bytes);
        }
        Look {
            gate: self.clone(),
            at,
            owners: owners.is_some(),
            ahead: Vec::new(),
            chosen: Vec::new(),
        }
    }
}

/// A start's look at the lanes, counted at the gate until it is dropped.
pub(super) struct Look {
    gate: Arc<Gate>,
    at: u64,
    owners: bool,
    /// The owner's starts this one left room for when it chose.
    ahead: Vec<u64>,
    /// The lanes it chose and has not settled yet.
    chosen: Vec<Id>,
}

impl Look {
    /// Chooses under the gate, `lanes` being every lane the look counted. An owner's start stops
    /// being ahead in the same moment, so no other start sees it both as chosen and as ahead.
    /// `choose` is given what the start sees, and says what it decided and the lanes it chose,
    /// by the instance it looked at, which are marked as leaving.
    pub(super) fn choose<T>(
        &mut self,
        lanes: &[Counted],
        choose: impl FnOnce(&Seen<'_>) -> (T, Vec<(PathBuf, Instance)>),
    ) -> T {
        let mut state = self.gate.state();
        state.prune();
        if self.owners {
            state.ahead.remove(&self.at);
            self.ahead.clear();
        } else {
            self.ahead = state.ahead.keys().copied().collect();
        }
        let seen = Seen {
            marks: &state.marks,
            ahead: state.ahead_of(&self.ahead),
            since: state.since(self.at, lanes),
        };
        let (decided, chosen) = choose(&seen);
        for (socket, instance) in chosen {
            self.chosen.push(instance.id);
            state.marks.insert(
                instance.id,
                Mark {
                    socket,
                    left: false,
                    gone_at: None,
                },
            );
        }
        decided
    }

    /// The lanes chosen have been asked to leave, each by its instance, with whether it left; and
    /// the start decides, under the gate, with what happened since it looked (the lanes it saw
    /// leave among it) and the owner's starts it left room for that still have not chosen. One
    /// that did not leave is unmarked, and may be chosen again; one that left stays marked. Only
    /// the mark of the instance asked is touched. A start decided for, or one that is `needed`,
    /// is let start: its lane at `own`, counted at `bytes`, counts for every other start until
    /// it listens or its launch fails ([`Admission`]).
    pub(super) fn settle(
        mut self,
        lanes: &[Counted],
        asked: &[(PathBuf, Instance, bool)],
        decide: impl FnOnce(Since, Coming) -> bool,
        (own, bytes): (&Path, u64),
        needed: bool,
    ) -> Option<Admission> {
        let mut state = self.gate.state();
        for (socket, instance, left) in asked {
            if *left {
                state
                    .marks
                    .entry(instance.id)
                    .or_insert_with(|| Mark {
                        socket: socket.clone(),
                        left: false,
                        gone_at: None,
                    })
                    .left = true;
            } else if state.marks.get(&instance.id).is_some_and(|mark| !mark.left) {
                state.marks.remove(&instance.id);
            }
        }
        self.chosen.clear();
        state.prune();
        let ahead = state.ahead_of(&self.ahead);
        let since = state.since(self.at, lanes);
        if !(decide(since, ahead) || needed) {
            return None;
        }
        state.admitted.insert(
            self.at,
            Admitted {
                socket: own.to_owned(),
                bytes,
                visible_at: None,
            },
        );
        Some(Admission {
            gate: self.gate.clone(),
            at: self.at,
            visible: false,
        })
    }
}

impl Drop for Look {
    /// A start that ends without settling (it panicked) unmarks what it chose and did not ask,
    /// so those lanes may be chosen again.
    fn drop(&mut self) {
        let mut state = self.gate.state();
        state.looks.remove(&self.at);
        state.ahead.remove(&self.at);
        for id in self.chosen.drain(..) {
            if state.marks.get(&id).is_some_and(|mark| !mark.left) {
                state.marks.remove(&id);
            }
        }
    }
}

/// A lane let start: it counts for every other start until it listens where any later look sees
/// it ([`Self::visible`]), and stops counting at once when it is dropped before that (its launch
/// failed, or was given up).
pub(crate) struct Admission {
    gate: Arc<Gate>,
    at: u64,
    visible: bool,
}

impl Admission {
    /// The lane said it is ready: it listens on its socket. Looks begun before now may not have
    /// seen it, and still count it; it is forgotten once none of them is open.
    pub(crate) fn visible(mut self) {
        let mut state = self.gate.state();
        let now = state.next();
        if let Some(admitted) = state.admitted.get_mut(&self.at) {
            admitted.visible_at = Some(now);
        }
        state.prune();
        self.visible = true;
    }
}

impl Drop for Admission {
    fn drop(&mut self) {
        if !self.visible {
            self.gate.state().admitted.remove(&self.at);
        }
    }
}

impl std::fmt::Debug for Admission {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Admission")
            .field("at", &self.at)
            .field("visible", &self.visible)
            .finish()
    }
}
