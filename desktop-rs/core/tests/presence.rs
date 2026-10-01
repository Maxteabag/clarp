use clarp_core::presence::{Presence, Report, eligible};

fn last_presence(reports: &[Report]) -> Option<bool> {
    reports.iter().rev().find_map(|r| if let Report::Presence { active, .. } = r { Some(*active) } else { None })
}

fn last_activity(reports: &[Report]) -> Option<bool> {
    reports.iter().rev().find_map(|r| if let Report::Activity { foreground, .. } = r { Some(*foreground) } else { None })
}

#[test]
fn eligibility_expires_without_user_input() {
    assert!(eligible(true, true, true, 0));
    assert!(eligible(true, true, true, 119_999));
    assert!(!eligible(true, true, true, 120_000));
    assert!(!eligible(true, true, true, -1));
    assert!(!eligible(false, true, true, 1));
    assert!(!eligible(true, false, true, 1));
    assert!(!eligible(true, true, false, 1));
}

#[test]
fn maintenance_activity_does_not_depend_on_push_preference() {
    let mut presence = Presence::default();
    let mut reports = presence.set_enabled(false, 0);
    reports.extend(presence.set_connected(true, 0));
    reports.extend(presence.set_session_state(true, true, 0));
    reports.extend(presence.set_foreground(true, 10));
    reports.extend(presence.note_interaction(20));
    assert_eq!(last_activity(&reports), Some(true), "in front and used");
    assert!(!presence.active(), "but alerts stay on: the preference is off");
    let reports = presence.set_foreground(false, 30);
    assert_eq!(last_activity(&reports), Some(false));
}

#[test]
fn focus_lock_sleep_and_preference_release_presence() {
    let mut presence = Presence::default();
    presence.set_session_state(true, true, 0);
    presence.set_connected(true, 0);
    // Coming to the front counts as a touch, as the C++ activeChanged does.
    let mut reports = presence.set_foreground(true, 0);
    reports.extend(presence.note_interaction(5));
    assert!(presence.active());
    assert_eq!(last_presence(&reports), Some(true));
    assert_eq!(last_presence(&presence.set_session_state(true, false, 6)), Some(false), "locking releases at once");
    presence.set_session_state(true, true, 7);
    presence.note_interaction(8);
    assert!(presence.active());
    presence.set_enabled(false, 9);
    assert!(!presence.active());
    presence.set_enabled(true, 10);
    assert!(presence.active());
    presence.prepare_for_sleep(true, 11);
    assert!(!presence.active());
    presence.prepare_for_sleep(false, 12);
    assert!(!presence.active(), "fresh input required after resume");
    presence.note_interaction(13);
    assert!(presence.active());
    presence.set_connected(false, 14);
    assert!(!presence.active());
    presence.set_connected(true, 15);
    let reports = presence.set_foreground(false, 16);
    assert!(!presence.active());
    assert_eq!(last_presence(&reports), Some(false));
}

#[test]
fn leases_renew_while_active_and_input_expires() {
    let mut presence = Presence::default();
    presence.set_session_state(true, true, 0);
    presence.set_connected(true, 0);
    presence.set_foreground(true, 0);
    presence.note_interaction(0);
    assert!(presence.refresh(5_000).is_empty(), "nothing new within the renewal period");
    assert!(presence.refresh(10_000).iter().any(|r| matches!(r, Report::Presence { active: true, .. })), "the lease renews");
    assert_eq!(last_presence(&presence.refresh(120_000)), Some(false), "two minutes without input ends it");
}

#[test]
fn unknown_session_never_suppresses() {
    let mut presence = Presence::default();
    presence.set_connected(true, 0);
    presence.set_foreground(true, 0);
    presence.note_interaction(1);
    assert!(!presence.active());
}
