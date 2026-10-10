//! The shared animation clock (`Motion` in theme.slint): every spinner,
//! shimmer, busy dot and breathing ring reads it instead of
//! `animation-tick()`. A binding on `animation-tick()` keeps Slint drawing
//! at the display's rate for as long as it lives, and each of those frames
//! walks the whole window (180 explorer rows and two chats on a busy fleet):
//! on the software renderer that saturated the UI thread.
//!
//! The clock has two hands, moved on alternate ticks so that no frame
//! repaints both: `spin` (the explorer's spinners and shimmering names,
//! about ten times a second) and `glow` (what animates in a chat: the
//! running row's shimmer, the typing dots; about twenty). The renderer
//! repaints at most three rectangles, merging the rest: an explorer row and
//! a chat row moving in the same frame became one box over everything
//! between them. Neither hand moves while the window is hidden or motion is
//! reduced. While a chat scrolls (`held`) each moves twice a second: every
//! scroll frame repaints the chat, and a spinner moving in the same frame
//! merged with it into a box over most of the window.

use std::cell::{Cell, RefCell};
use std::time::{Duration, Instant};

use slint::ComponentHandle;

/// The clock's tick, about a 60 Hz frame; each hand moves on its own ticks
/// (`hand`). A 25 ms tick fell between the chat's 16 ms timers and moved
/// where a send's flight was sampled against its bubble.
pub const TICK: Duration = Duration::from_millis(17);

/// Which hand moves on tick `n`: `glow` on every third (51 ms, ~20 a
/// second), `spin` on every sixth, offset (102 ms, ~10), never both.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Hand {
    Spin,
    Glow,
}

pub fn hand(n: u64) -> Option<Hand> {
    if n % 3 == 1 {
        Some(Hand::Glow)
    } else if n % 6 == 3 {
        Some(Hand::Spin)
    } else {
        None
    }
}

/// Which hand moves on tick `n` while a chat scrolls: each every thirtieth
/// (510 ms), apart.
pub fn held_hand(n: u64) -> Option<Hand> {
    match n % 30 {
        1 => Some(Hand::Glow),
        16 => Some(Hand::Spin),
        _ => None,
    }
}

thread_local! {
    static TIMER: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
    static ORIGIN: Instant = Instant::now();
    static TICKS: Cell<u64> = const { Cell::new(0) };
    /// Since when the clock has been held.
    static HELD: Cell<Option<Instant>> = const { Cell::new(None) };
}

/// A hold lasts this long at most: a pane closed while it scrolled leaves
/// no one to let go.
const HOLD_AT_MOST: Duration = Duration::from_secs(3);

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
    if hand(n).is_none() && held_hand(n).is_none() {
        return;
    }
    let Some(window) = crate::window() else { return };
    let look = window.global::<crate::ChatLook>();
    if !look.get_window_shown() || look.get_reduced_motion() {
        return;
    }
    let motion = window.global::<crate::Motion>();
    if motion.get_held() {
        let since = HELD.with(|h| {
            let since = h.get().unwrap_or_else(Instant::now);
            h.set(Some(since));
            since
        });
        if since.elapsed() > HOLD_AT_MOST {
            motion.set_held(false);
        }
    } else {
        HELD.with(|h| h.set(None));
    }
    let Some(hand) = (if motion.get_held() { held_hand(n) } else { hand(n) }) else { return };
    let now = ORIGIN.with(Instant::elapsed).as_millis() as i64;
    match hand {
        Hand::Spin => motion.set_spin(now),
        Hand::Glow => motion.set_glow(now),
    }
}

#[cfg(test)]
mod tests {
    use super::{Hand, TICK, hand, held_hand};

    #[test]
    fn while_a_chat_scrolls_each_hand_moves_twice_a_second_apart() {
        let ticks = 6000 / TICK.as_millis() as u64;
        let hands: Vec<Option<Hand>> = (1..=ticks).map(held_hand).collect();
        let spins = hands.iter().filter(|h| **h == Some(Hand::Spin)).count();
        let glows = hands.iter().filter(|h| **h == Some(Hand::Glow)).count();
        assert!((11..=12).contains(&spins), "{spins} spins in 6 s");
        assert!((11..=12).contains(&glows), "{glows} glows in 6 s");
    }

    #[test]
    fn spin_moves_ten_times_a_second_glow_twenty_and_never_together() {
        // Six seconds of ticks: at most 10 and 20 moves a second.
        let ticks = 6000 / TICK.as_millis() as u64;
        let hands: Vec<Option<Hand>> = (1..=ticks).map(hand).collect();
        let spins = hands.iter().filter(|h| **h == Some(Hand::Spin)).count();
        let glows = hands.iter().filter(|h| **h == Some(Hand::Glow)).count();
        assert!((55..=60).contains(&spins), "{spins} spins in 6 s");
        assert!((110..=120).contains(&glows), "{glows} glows in 6 s");
        assert_eq!(hands[..6], [Some(Hand::Glow), None, Some(Hand::Spin), Some(Hand::Glow), None, None]);
    }
}
