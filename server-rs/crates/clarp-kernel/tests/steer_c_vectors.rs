//! Claude's stream records held to the isolated Python reference.

use clarp_kernel::steering::{Observation, Stream, tool_changes};
use serde_json::Value;

#[test]
#[allow(clippy::unwrap_used)]
fn claudes_tool_windows_and_replay_echoes_follow_the_reference() {
    let fixture: Value = serde_json::from_str(include_str!("fixtures/steer-c-vectors.json")).unwrap();
    let mut records = 0;
    for case in fixture["classifier"].as_array().unwrap() {
        let changed = tool_changes(&case["event"]);
        let expected = |key| {
            case[key].as_array().unwrap().iter().map(|id| id.as_str().unwrap()).collect::<Vec<_>>()
        };
        assert_eq!(changed.opened, expected("opened"), "{case}");
        assert_eq!(changed.answered, expected("answered"), "{case}");
        records += 1;
    }
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
            assert_eq!(Some(stream.can_join()), step["can_offer"].as_bool(), "{}: {step}", case["name"]);
            steps += 1;
        }
        streams += 1;
    }
    assert_eq!((records, streams, steps), (23, 6, 27));
}
