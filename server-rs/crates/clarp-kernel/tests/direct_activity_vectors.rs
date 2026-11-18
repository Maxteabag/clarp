#![allow(clippy::unwrap_used)]
use clarp_kernel::alert_routes::{ActivityRoutes, Path};
use serde_json::{Value, json};

#[test]
fn one_activity_reservation_is_never_used_for_two_uncertain_sends() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/direct-activity-vectors.json")).unwrap();
    let mut steps = 0;
    for row in rows {
        let mut routes = ActivityRoutes::new(
            row["mode"].as_str().unwrap(),
            row["account"] == true,
            row["key"] == true,
        );
        let bind = row["bind"].as_i64().unwrap();
        let send = row["send"].as_i64().unwrap();
        for step in row["steps"].as_array().unwrap() {
            let mut attempts = Vec::new();
            let (status, reason) = match routes.path() {
                Some(Path::Account) => {
                    let relay = routes.try_relay();
                    if relay && bind == 200 {
                        attempts.push("account");
                    }
                    if !relay
                        || bind >= 500
                        || (bind == 200 && (send >= 500 || matches!(send, 404 | 405)))
                    {
                        if routes.outage(relay && bind == 200 && send >= 500) {
                            attempts.push("direct");
                            (200, String::new())
                        } else {
                            (0, "down".into())
                        }
                    } else if bind != 200 {
                        (401, "RelayRegistrationRejected".into())
                    } else if send != 200 {
                        (send, format!("Relay{send}"))
                    } else {
                        (200, String::new())
                    }
                }
                Some(Path::Direct) => {
                    attempts.push("direct");
                    (200, String::new())
                }
                None => (0, "NoPushPath".into()),
                Some(Path::Grant) => panic!("an activity token carries no grant"),
            };
            assert_eq!(json!([attempts, status, reason]), *step, "{row}");
            steps += 1;
        }
    }
    assert_eq!(steps, 336);
}
