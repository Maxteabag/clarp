//! Is someone at this desktop (C++ `DesktopPresence`)? While the window is
//! in front, the session unlocked and awake, and someone touched it in the
//! last two minutes, the Host may pause phone alerts. Two reports come out:
//! `Presence` leases (renewed every 10 s while active, released at once) and
//! `Activity` (foreground and input age, for maintenance scheduling, which
//! does not depend on the push preference). An unknown session state never
//! suppresses alerts.

pub const INPUT_WINDOW_MS: i64 = 120_000;
pub const RENEW_MS: i64 = 10_000;
/// Input age reported when there was none: a day.
pub const NO_INPUT_MS: i64 = 86_400_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Report {
    Presence { sequence: u64, active: bool },
    Activity { sequence: u64, foreground: bool, input_age_ms: i64 },
}

pub fn eligible(enabled: bool, foreground: bool, unlocked: bool, input_age_ms: i64) -> bool {
    enabled && foreground && unlocked && (0..INPUT_WINDOW_MS).contains(&input_age_ms)
}

#[derive(Debug)]
pub struct Presence {
    enabled: bool,
    connected: bool,
    session_available: bool,
    unlocked: bool,
    sleeping: bool,
    foreground: bool,
    last_input: Option<i64>,
    active: bool,
    reported: bool,
    reported_foreground: bool,
    sequence: u64,
    activity_sequence: u64,
    last_report: i64,
    last_activity_report: i64,
}

impl Default for Presence {
    fn default() -> Self {
        Self {
            enabled: true,
            connected: false,
            session_available: false,
            unlocked: false,
            sleeping: false,
            foreground: false,
            last_input: None,
            active: false,
            reported: false,
            reported_foreground: false,
            sequence: 0,
            activity_sequence: 0,
            last_report: -RENEW_MS,
            last_activity_report: -RENEW_MS,
        }
    }
}

impl Presence {
    pub fn active(&self) -> bool {
        self.active
    }
    pub fn session_available(&self) -> bool {
        self.session_available
    }

    pub fn set_enabled(&mut self, enabled: bool, now: i64) -> Vec<Report> {
        self.enabled = enabled;
        self.refresh(now)
    }
    pub fn set_connected(&mut self, connected: bool, now: i64) -> Vec<Report> {
        self.connected = connected;
        self.refresh(now)
    }
    pub fn set_session_state(&mut self, available: bool, unlocked: bool, now: i64) -> Vec<Report> {
        self.session_available = available;
        self.unlocked = unlocked;
        self.refresh(now)
    }
    /// The window is in front (active, visible, not minimized).
    pub fn set_foreground(&mut self, foreground: bool, now: i64) -> Vec<Report> {
        if foreground && !self.foreground {
            // Coming to the front is itself a touch.
            self.last_input = Some(now);
        }
        self.foreground = foreground;
        self.refresh(now)
    }
    pub fn note_interaction(&mut self, now: i64) -> Vec<Report> {
        self.last_input = Some(now);
        self.refresh(now)
    }
    /// Suspend; after resume, fresh input is needed again.
    pub fn prepare_for_sleep(&mut self, sleeping: bool, now: i64) -> Vec<Report> {
        self.sleeping = sleeping;
        if !sleeping {
            self.last_input = None;
        }
        self.refresh(now)
    }

    pub fn refresh(&mut self, now: i64) -> Vec<Report> {
        let mut reports = Vec::new();
        let awake = self.session_available && self.unlocked && !self.sleeping;
        let usable = self.foreground && self.connected && awake;
        let input_age = self.last_input.map(|at| now - at);
        if usable != self.reported_foreground || (usable && now - self.last_activity_report >= RENEW_MS) {
            self.reported_foreground = usable;
            self.last_activity_report = now;
            self.activity_sequence += 1;
            reports.push(Report::Activity {
                sequence: self.activity_sequence,
                foreground: usable,
                input_age_ms: input_age.map_or(NO_INPUT_MS, |age| age.min(NO_INPUT_MS)),
            });
        }
        self.active = eligible(self.enabled && self.connected, self.foreground, awake, input_age.unwrap_or(-1));
        // Leaving active releases at once; only active use renews the lease.
        if (self.sequence == 0 && self.active) || self.active != self.reported || (self.active && now - self.last_report >= RENEW_MS) {
            self.last_report = now;
            self.reported = self.active;
            self.sequence += 1;
            reports.push(Report::Presence { sequence: self.sequence, active: self.active });
        }
        reports
    }
}
