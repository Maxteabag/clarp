//! Startup with a real Host's roster (`--check startup`): the fake Host
//! serves 100 agents (`fake_host.py --roster 100`: helpers under parents,
//! busy and waiting agents, attention items, background jobs, a 512 px
//! portrait each). Measured with the app's own clock (`perf`): launch to
//! the explorer listing every agent, to every portrait shown and to the
//! first chat's rows; what each chat-list rebuild and each engine wake
//! cost the UI thread; and how many rebuilds the first seconds and a burst
//! of Host events cause.

use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use serde_json::json;

use super::{Stage, check, control, run_stages, shot};
use crate::perf::{self, Event, ms};

/// Budgets for a debug build (the checks' build) with 100 agents, with
/// room for a slower machine than the one measured (elitebook; xps is
/// about 1.6 times slower). 7d5286a7 and a56deecb exceed all but the
/// first by far (see the commit adding this check).
///
/// The explorer lists every agent this soon after launch: the fake Host
/// answers at once, so this is the app's own work (113-239 ms).
const EXPLORER: Duration = Duration::from_millis(500);
/// The first chat's rows are drawn this soon after launch (65 ms with two
/// agents; the roster's size must not hold up the open chat).
const FIRST_CHAT: Duration = Duration::from_millis(1000);
/// A usual chat-list rebuild of 100 rows (the median): a 60 Hz frame has
/// 16 ms and the list is one of many things in it (2-4 ms; was 8-14).
const REBUILD: Duration = Duration::from_millis(6);
/// The longest rebuild: never a visible pause (5-10 ms; was 300-520).
const LONGEST_REBUILD: Duration = Duration::from_millis(25);
/// The longest the UI thread may be held by one engine wake: a few
/// dropped frames, never a visible freeze (opening the first chat, 36-88
/// ms; was 0.3-12 s).
const WAKE: Duration = Duration::from_millis(200);
/// Rebuilds in the first five seconds: the roster, the selection, rooms,
/// archive and updates each change the list, and portraits arrive at most
/// ten times a second, not one rebuild each (7-13).
const STARTUP_REBUILDS: usize = 15;
/// Rebuilds for a burst of 40 Host events delivered together: the list is
/// rebuilt once per wake, not once per event.
const BURST_REBUILDS: usize = 10;
/// Ctrl+R's recent agents, sorted by activity (0.2 ms; was 1.3 s).
const RECENT: Duration = Duration::from_millis(10);

fn median(events: &[Event]) -> Duration {
    let mut took: Vec<Duration> = events.iter().map(|e| e.took).collect();
    took.sort();
    took.get(took.len() / 2).copied().unwrap_or_default()
}

fn summary(name: &str, events: &[Event]) -> String {
    let total: Duration = events.iter().map(|e| e.took).sum();
    let longest = events.iter().max_by_key(|e| e.took);
    format!(
        "perf {name}: {} taking {:.1} ms in all, the longest {:.2} ms ({})",
        events.len(),
        ms(total),
        longest.map_or(0.0, |e| ms(e.took)),
        longest.map_or(String::new(), |e| e.what.clone()),
    )
}

pub(super) fn startup_check(out: String) {
    let burst_from = Rc::new(Cell::new(0usize));
    let burst_from2 = burst_from.clone();
    let stages: Vec<Stage> = vec![
        ("settled", Box::new(move |_, _, _| {
            // The first five seconds: portraits, updates and the open chat.
            if perf::now() < Duration::from_secs(5) {
                return false;
            }
            let stats = perf::stats();
            let at = |d: Option<Duration>| d.map_or("never".to_owned(), |d| format!("{} ms", d.as_millis()));
            println!(
                "perf startup: roster at {}, explorer complete at {}, portraits at {}, first chat at {}",
                at(stats.snapshot), at(stats.explorer_complete), at(stats.portraits_complete), at(stats.first_chat)
            );
            println!("{}", summary("rebuilds", &stats.rebuilds));
            println!("{}", summary("wakes", &stats.wakes));
            println!("{}", summary("frames", &stats.frames));
            check(
                stats.explorer_complete.is_some_and(|d| d <= EXPLORER),
                &format!("the explorer lists all agents {:?} after launch (budget {EXPLORER:?})", stats.explorer_complete),
            );
            check(stats.portraits_complete.is_some(), "every agent's portrait shows within five seconds");
            check(
                stats.first_chat.is_some_and(|d| d <= FIRST_CHAT),
                &format!("the first chat has rows {:?} after launch (budget {FIRST_CHAT:?})", stats.first_chat),
            );
            let usual = median(&stats.rebuilds);
            check(usual <= REBUILD, &format!("a chat-list rebuild takes under {REBUILD:?} (median {:.2} ms)", ms(usual)));
            let longest = stats.rebuilds.iter().map(|e| e.took).max().unwrap_or_default();
            check(longest <= LONGEST_REBUILD, &format!("none over {LONGEST_REBUILD:?} (longest {:.2} ms)", ms(longest)));
            let wake = stats.wakes.iter().max_by_key(|e| e.took);
            check(
                wake.is_none_or(|e| e.took <= WAKE),
                &format!("no engine wake holds the UI thread over {WAKE:?} (longest {:.2} ms: {})", wake.map_or(0.0, |e| ms(e.took)), wake.map_or("", |e| &e.what)),
            );
            check(
                stats.rebuilds.len() <= STARTUP_REBUILDS,
                &format!("{} chat-list rebuilds in the first five seconds (budget {STARTUP_REBUILDS})", stats.rebuilds.len()),
            );
            burst_from.set(stats.rebuilds.len());
            // 40 events at once: agents start and stop, some have news.
            let mut sent = true;
            for i in 0..40 {
                let session = format!("agent-{}", 10 + i * 2);
                let event = if i % 4 == 3 {
                    json!({"type": "user-notification", "session": session, "persona": session, "preview": "news"})
                } else {
                    json!({"type": "agent-state", "session": session, "kind": if i % 2 == 0 { "thinking" } else { "idle" }})
                };
                sent &= control("/__control/event", &event).is_ok();
            }
            check(sent, "the Host sends a burst of 40 events");
            true
        })),
        ("burst", Box::new(move |_, _, elapsed| {
            if elapsed < Duration::from_secs(2) {
                return false;
            }
            let stats = perf::stats();
            let burst = &stats.rebuilds[burst_from2.get()..];
            println!("{}", summary("burst rebuilds", burst));
            let frames: Vec<Event> = stats.frames.iter().filter(|f| f.at >= Duration::from_secs(5)).cloned().collect();
            println!("{}", summary("frames since", &frames));
            check(burst.len() <= BURST_REBUILDS, &format!("40 events rebuild the chat list {} times (budget {BURST_REBUILDS})", burst.len()));
            let longest = burst.iter().map(|e| e.took).max().unwrap_or_default();
            check(longest <= LONGEST_REBUILD, &format!("no rebuild in the burst over {LONGEST_REBUILD:?} (longest {:.2} ms)", ms(longest)));
            true
        })),
        ("recent", Box::new(move |app, _, _| {
            let started = std::time::Instant::now();
            let recent = crate::switcher::recent(&app.engine.borrow(), &[], "");
            let took = started.elapsed();
            check(
                recent.len() > 50 && took <= RECENT,
                &format!("Ctrl+R lists {} recent agents in {:.2} ms (budget {RECENT:?})", recent.len(), ms(took)),
            );
            true
        })),
        ("shot", Box::new(move |_, _, _| {
            shot(&out, "startup-01-100-agents");
            true
        })),
    ];
    run_stages(stages);
}
