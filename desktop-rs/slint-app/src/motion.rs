//! The shared animation clock (`Motion` in theme.slint): every spinner,
//! shimmer, busy dot and breathing ring reads it instead of
//! `animation-tick()`. A binding on `animation-tick()` keeps Slint drawing
//! at the display's rate for as long as it lives, and each of those frames
//! walks the whole window (180 explorer rows and two chats on a busy fleet):
//! on the software renderer that saturated the UI thread.
//!
//! The clock has two hands, moved on alternate ticks so that no frame
//! repaints both: `spin` (the explorer's spinners and shimmering names,
//! ten times a second) and `glow` (what animates in a chat: the running
//! row's shimmer, the typing dots; twenty times a second). The renderer
//! repaints at most three rectangles, merging the rest: an explorer row and
//! a chat row moving in the same frame became one box over everything
//! between them. Neither hand moves while the window is hidden or motion is
//! reduced.

use std::cell::{Cell, RefCell};
use std::time::{Duration, Instant};

use slint::ComponentHandle;

/// The clock's tick; each hand moves on its own ticks (`hand`).
pub const TICK: Duration = Duration::from_millis(25);

/// Which hand moves on tick `n`: `glow` on odd ticks (every 50 ms), `spin`
/// on every fourth (every 100 ms), never both.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Hand {
    Spin,
    Glow,
}

pub fn hand(n: u64) -> Option<Hand> {
    if n % 2 == 1 {
        Some(Hand::Glow)
    } else if n % 4 == 0 {
        Some(Hand::Spin)
    } else {
        None
    }
}

thread_local! {
    static TIMER: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
    static ORIGIN: Instant = Instant::now();
    static TICKS: Cell<u64> = const { Cell::new(0) };
}

/// Starts the clock (once the window exists).
pub fn start() {
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, TICK, tick);
    TIMER.with(|t| *t.borrow_mut() = Some(timer));
}

fn tick() {
    let n = TICKS.with(|t| {
        t.set(t.get() + 1);
        t.get()
    });
    let Some(hand) = hand(n) else { return };
    let Some(window) = crate::window() else { return };
    let look = window.global::<crate::ChatLook>();
    if !look.get_window_shown() || look.get_reduced_motion() {
        return;
    }
    let now = ORIGIN.with(Instant::elapsed).as_millis() as i64;
    let motion = window.global::<crate::Motion>();
    match hand {
        Hand::Spin => motion.set_spin(now),
        Hand::Glow => motion.set_glow(now),
    }
}

#[cfg(test)]
mod tests {
    use super::{Hand, TICK, hand};

    #[test]
    fn spin_moves_ten_times_a_second_glow_twenty_and_never_together() {
        let per_second = (1000 / TICK.as_millis()) as u64;
        let hands: Vec<Option<Hand>> = (1..=per_second).map(hand).collect();
        assert_eq!(hands.iter().filter(|h| **h == Some(Hand::Spin)).count(), 10);
        assert_eq!(hands.iter().filter(|h| **h == Some(Hand::Glow)).count(), 20);
        assert_eq!((hand(1), hand(2), hand(3), hand(4)), (Some(Hand::Glow), None, Some(Hand::Glow), Some(Hand::Spin)));
    }
}
