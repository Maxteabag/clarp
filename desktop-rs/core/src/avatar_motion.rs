//! The shared clock behind avatar "working" pulses and process glyphs (C++
//! `AvatarMotionClock`). One clock for every visible avatar: `revision`
//! advances per animation frame (20 fps) only while something visible is
//! working, `process_revision` per process hop (350 ms), and
//! `working_revision` when the working set, reduced motion or foreground
//! changes, so "is this agent working" bindings are not re-evaluated per
//! frame. This is the Qt-free state; the bridge owns the timers.

use std::collections::{HashMap, HashSet};

pub const FRAME_MS: u64 = 50;
pub const PROCESS_MS: u64 = 350;
pub const PULSE_MS: u64 = 2400;

/// Which timers should run.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Timers {
    pub frame: bool,
    pub process: bool,
}

#[derive(Debug)]
pub struct MotionClock {
    foreground: bool,
    /// A watched window is visible and not minimized (true with none).
    window_exposed: bool,
    observers: HashSet<usize>,
    process_observers: HashSet<usize>,
    /// Session -> when it started working (ms on the caller's clock).
    epochs: HashMap<String, u64>,
    reduced: bool,
    pub revision: u64,
    pub process_revision: u64,
    pub working_revision: u64,
}

impl MotionClock {
    pub fn new(reduced: bool) -> Self {
        Self {
            foreground: true,
            window_exposed: true,
            observers: HashSet::new(),
            process_observers: HashSet::new(),
            epochs: HashMap::new(),
            reduced,
            revision: 0,
            process_revision: 0,
            working_revision: 0,
        }
    }

    pub fn reduced(&self) -> bool {
        self.reduced
    }

    fn can_animate_process(&self) -> bool {
        self.window_exposed
    }

    fn can_animate(&self) -> bool {
        self.foreground && self.can_animate_process()
    }

    pub fn timers(&self) -> Timers {
        Timers {
            frame: self.can_animate() && !self.reduced && !self.observers.is_empty() && !self.epochs.is_empty(),
            process: self.can_animate_process() && !self.reduced && !self.process_observers.is_empty(),
        }
    }

    fn bump(&mut self) {
        self.revision += 1;
        self.working_revision += 1;
    }

    /// An avatar (by identity) wants frames while it is visible and working.
    pub fn observe(&mut self, owner: usize, visible: bool) {
        if visible {
            self.observers.insert(owner);
        } else {
            self.observers.remove(&owner);
        }
    }

    pub fn observe_process(&mut self, owner: usize, visible: bool) {
        if visible {
            self.process_observers.insert(owner);
        } else {
            self.process_observers.remove(&owner);
        }
    }

    /// Returns whether it changed (the caller persists and notifies).
    pub fn set_reduced(&mut self, reduced: bool) -> bool {
        if self.reduced == reduced {
            return false;
        }
        self.reduced = reduced;
        self.bump();
        true
    }

    pub fn set_foreground(&mut self, foreground: bool) {
        self.foreground = foreground;
        self.bump();
    }

    /// Returns whether exposure changed.
    pub fn set_window_exposed(&mut self, exposed: bool) -> bool {
        if self.window_exposed == exposed {
            return false;
        }
        self.window_exposed = exposed;
        self.bump();
        self.process_revision += 1;
        true
    }

    /// The sessions working now; a session keeps its pulse phase while it
    /// stays working.
    pub fn reconcile(&mut self, active: &HashSet<String>, now_ms: u64) {
        self.epochs.retain(|session, _| active.contains(session));
        for session in active {
            self.epochs.entry(session.clone()).or_insert(now_ms);
        }
        self.bump();
    }

    pub fn working(&self, session: &str) -> bool {
        self.epochs.contains_key(session)
    }

    pub fn phase(&self, session: &str, now_ms: u64) -> f64 {
        match self.epochs.get(session) {
            Some(start) if !self.reduced && self.can_animate() => (now_ms.saturating_sub(*start) % PULSE_MS) as f64 / PULSE_MS as f64,
            _ => 0.0,
        }
    }

    pub fn process_hop(&self) -> i32 {
        if self.reduced || !self.can_animate_process() {
            return 0;
        }
        match self.process_revision % 4 {
            1 | 3 => -1,
            2 => -2,
            _ => 0,
        }
    }
}
