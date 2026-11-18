#![allow(clippy::unwrap_used)]

use clarp_kernel::alert_routes::{Outcome, Path, Routes};
use serde_json::{Value, json};

#[test]
fn reference_alert_routes() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/push-direct-vectors.json")).unwrap();
    let mut steps = 0;
    for row in rows {
        let mut routes = Routes::new(
            row["mode"].as_str().unwrap(),
            row["relay"] == true,
            row["account"] == true,
            row["key"] == true,
        );
        assert_eq!(row["enabled"], routes.enabled(), "{row}");
        for step in row["steps"].as_array().unwrap() {
            let mut paths = Vec::new();
            let delivery = routes.send(step[0] == true, |path| {
                let (label, status) = match path {
                    Path::Grant => ("grant", row["grant_status"].as_u64().unwrap()),
                    Path::Account => ("account", row["account_status"].as_u64().unwrap()),
                    Path::Direct => ("direct", 200),
                };
                paths.push(label);
                match status {
                    200 => Outcome::Sent,
                    401 => Outcome::GrantRevoked,
                    503 => Outcome::Down(String::new()),
                    status => Outcome::Refused {
                        status: status as u16,
                        reason: format!("Relay{status}"),
                    },
                }
            });
            let (status, reason) = match delivery.outcome {
                Outcome::Sent => (200, ""),
                Outcome::GrantRevoked => (410, "GrantRevoked"),
                Outcome::NoPath => (0, "NoPushPath"),
                Outcome::Down(_) => (0, "down"),
                Outcome::Refused { status, ref reason } => (status, reason.as_str()),
                Outcome::Dead(_) => panic!("unexpected dead token"),
            };
            assert_eq!(
                json!([step[0], paths, delivery.grant_revoked, status, reason]),
                *step,
                "{row}"
            );
            steps += 1;
        }
    }
    assert_eq!(steps, 800);
}
