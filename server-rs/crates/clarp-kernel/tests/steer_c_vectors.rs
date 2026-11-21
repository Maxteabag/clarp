//! Claude's stream records held to the isolated Python reference.

#![allow(clippy::unwrap_used)]

use clarp_kernel::steering::{Observation, Stream};
use serde_json::Value;

#[test]
fn claudes_input_fence_and_replay_echoes_follow_the_reference() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/steer-c-vectors.json")).unwrap();
    let mut streams = 0;
    let mut steps = 0;
    for case in fixture["streams"].as_array().unwrap() {
        let mut stream = Stream::default();
        for step in case["steps"].as_array().unwrap() {
            let echo = match stream.observe(&step["event"]) {
                Observation::Echo(uuid) => Some(uuid),
                _ => None,
            };
            assert_eq!(echo, step["echo"].as_str(), "{}: {step}", case["name"]);
            assert_eq!(
                Some(stream.can_join()),
                step["can_offer"].as_bool(),
                "{}: {step}",
                case["name"]
            );
            steps += 1;
        }
        streams += 1;
    }
    assert_eq!((streams, steps), (7, 34));
}
