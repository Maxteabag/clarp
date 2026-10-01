use clarp_core::audio_journal::{COMPLETED_LIMIT, Journal, State, clip_key, service_name};
use clarp_core::json::Object;
use serde_json::json;

fn clip(id: i64) -> Object {
    json!({"clip_id": id, "url": format!("/clips/{id}.mp3")}).as_object().cloned().unwrap()
}

#[test]
fn offers_are_taken_once_in_order() {
    let mut journal = Journal::new(false);
    assert_eq!(journal.offer(&[clip(1), clip(2)]), [clip(1), clip(2)]);
    assert!(journal.offer(&[clip(1)]).is_empty(), "already pending");
    assert!(journal.begin(&clip(1)));
    assert!(!journal.begin(&clip(1)), "started once");
    journal.finish(&clip(1));
    assert!(journal.offer(&[clip(1)]).is_empty(), "never replayed after it finished");
    assert!(!journal.begin(&clip(9)), "unknown clip");
}

#[test]
fn a_new_owner_keeps_pending_clips_but_never_replays_a_started_one() {
    let mut journal = Journal::new(false);
    journal.offer(&[clip(1), clip(2), clip(3)]);
    journal.begin(&clip(2));
    let saved = journal.to_json();
    let (mut next, queued) = Journal::take_over(Some(&saved), false).unwrap();
    assert_eq!(queued, [clip(1), clip(3)], "clip 2 had started when its owner went away");
    assert!(next.offer(&[clip(2)]).is_empty(), "and it counts as completed");
    assert!(Journal::take_over(Some("not json"), false).is_err(), "an invalid journal keeps playback stopped");
    let (fresh, queued) = Journal::take_over(None, true).unwrap();
    assert!(fresh.muted() && queued.is_empty());
}

#[test]
fn a_muted_journal_takes_nothing_and_completed_is_bounded() {
    let mut journal = Journal::new(true);
    assert!(journal.offer(&[clip(1)]).is_empty());
    journal.set_muted(false);
    for id in 1..=(COMPLETED_LIMIT as i64 + 10) {
        journal.finish(&clip(id));
    }
    assert_eq!(journal.offer(&[clip(5)]), [clip(5)], "the oldest completed keys are forgotten");
    assert!(journal.offer(&[clip(COMPLETED_LIMIT as i64)]).is_empty());
}

#[test]
fn keys_names_and_state_match_the_cpp_protocol() {
    assert_eq!(clip_key(&clip(7)), "7");
    let anonymous = json!({"url": "/x.mp3"}).as_object().cloned().unwrap();
    assert_eq!(clip_key(&anonymous).len(), 64);
    let name = service_name("http://127.0.0.1:7682\u{0}token");
    assert!(name.starts_with("com.maxteabag.Clarp.Audio.h") && name.len() == "com.maxteabag.Clarp.Audio.h".len() + 64);
    let state = State { muted: true, playing: false, paused: true, available: true };
    assert_eq!(State::from_json(&state.to_json()), Some(state));
}
