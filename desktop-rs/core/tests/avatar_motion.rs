use std::collections::HashSet;

use clarp_core::avatar_motion::{MotionClock, Timers};

fn set(items: &[&str]) -> HashSet<String> {
    items.iter().map(|s| s.to_string()).collect()
}

#[test]
fn frames_run_only_while_something_visible_is_working() {
    let mut clock = MotionClock::new(false);
    assert_eq!(clock.timers(), Timers::default());
    clock.observe(1, true);
    assert!(!clock.timers().frame, "nobody is working");
    clock.reconcile(&set(&["rachel"]), 1000);
    assert!(clock.timers().frame);
    clock.set_foreground(false);
    assert!(!clock.timers().frame, "a background app does not animate");
    clock.set_foreground(true);
    clock.observe(1, false);
    assert!(!clock.timers().frame, "no visible observer");
}

#[test]
fn a_working_session_keeps_its_phase_and_reduced_motion_stops_it() {
    let mut clock = MotionClock::new(false);
    clock.reconcile(&set(&["rachel"]), 1000);
    assert!(clock.working("rachel") && !clock.working("mike"));
    assert_eq!(clock.phase("rachel", 1000 + 600), 0.25);
    clock.reconcile(&set(&["rachel", "mike"]), 5000);
    assert_eq!(clock.phase("rachel", 1000 + 1200), 0.5, "an ongoing session keeps its start");
    let working = clock.working_revision;
    assert!(clock.set_reduced(true));
    assert!(!clock.set_reduced(true), "no change");
    assert!(clock.working_revision > working);
    assert_eq!(clock.phase("rachel", 1600), 0.0);
    clock.reconcile(&set(&[]), 6000);
    assert!(!clock.working("rachel"));
}

#[test]
fn process_glyph_hops_on_its_own_clock() {
    let mut clock = MotionClock::new(false);
    clock.observe_process(7, true);
    assert!(clock.timers().process && !clock.timers().frame);
    let hops: Vec<i32> = (0..4).map(|_| { clock.process_revision += 1; clock.process_hop() }).collect();
    assert_eq!(hops, [-1, -2, -1, 0]);
    assert!(clock.set_window_exposed(false));
    assert_eq!(clock.process_hop(), 0);
    assert!(!clock.timers().process, "a hidden window stops the glyph clock");
}
