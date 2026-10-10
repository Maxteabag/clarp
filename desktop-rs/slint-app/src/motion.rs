//! The shared animation clock (`Motion` in theme.slint): every spinner,
//! shimmer, busy dot and breathing ring reads it instead of
//! `animation-tick()`. A binding on `animation-tick()` keeps Slint drawing
//! at the display's rate for as long as it lives, and each of those frames
//! walks the whole window (180 explorer rows and two chats on a busy fleet):
//! on the software renderer that saturated the UI thread. This clock moves
//! `spin` ten times a second and `glow` twenty, so a frame is drawn only
//! when something moves, and stands still while the window is hidden or
//! motion is reduced.

use std::cell::RefCell;
use std::time::{Duration, Instant};

use slint::ComponentHandle;

/// How often the clock moves: `glow` every tick, `spin` every other one.
pub const TICK: Duration = Duration::from_millis(50);
const SPIN_EVERY: u128 = 2;

thread_local! {
    static TIMER: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
    static ORIGIN: Instant = Instant::now();
}

/// Starts the clock (once the window exists).
pub fn start() {
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, TICK, tick);
    TIMER.with(|t| *t.borrow_mut() = Some(timer));
}

/// The clock's readings `elapsed` after it started, in ms: (spin, glow),
/// each a whole number of its own steps.
pub fn readings(elapsed: Duration) -> (i64, i64) {
    let tick = TICK.as_millis();
    let ticks = elapsed.as_millis() / tick;
    let glow = ticks * tick;
    let spin = ticks / SPIN_EVERY * SPIN_EVERY * tick;
    (spin as i64, glow as i64)
}

fn tick() {
    let Some(window) = crate::window() else { return };
    let look = window.global::<crate::ChatLook>();
    if !look.get_window_shown() || look.get_reduced_motion() {
        return;
    }
    let (spin, glow) = readings(ORIGIN.with(Instant::elapsed));
    let motion = window.global::<crate::Motion>();
    // Setting an unchanged value would still mark what reads it as dirty.
    if motion.get_glow() != glow {
        motion.set_glow(glow);
    }
    if motion.get_spin() != spin {
        motion.set_spin(spin);
    }
}

#[cfg(test)]
mod tests {
    use super::readings;
    use std::time::Duration;

    #[test]
    fn spin_moves_ten_times_a_second_and_glow_twenty() {
        let at = |ms| readings(Duration::from_millis(ms));
        assert_eq!(at(0), (0, 0));
        assert_eq!(at(49), (0, 0));
        assert_eq!(at(50), (0, 50));
        assert_eq!(at(99), (0, 50));
        assert_eq!(at(100), (100, 100));
        assert_eq!(at(1234), (1200, 1200));
        assert_eq!(at(1260), (1200, 1250));
        let glows: std::collections::BTreeSet<i64> = (0..1000).map(|ms| at(ms).1).collect();
        let spins: std::collections::BTreeSet<i64> = (0..1000).map(|ms| at(ms).0).collect();
        assert_eq!((glows.len(), spins.len()), (20, 10));
    }
}
