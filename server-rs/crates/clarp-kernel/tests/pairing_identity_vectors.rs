//! The Python pairing handlers recorded with a fixed clock and a disposable device directory.

#![allow(clippy::unwrap_used)]

use clarp_kernel::devices::{self, RevokeRefusal};
use clarp_kernel::trust::{self, FailureLimiter};
use serde_json::Value;

#[test]
fn pairing_budgets_and_revocation_match_the_reference() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/pairing-identity-vectors.json")).unwrap();
    let mut identity = FailureLimiter::new(
        trust::IDENTITY_READS_MOST,
        trust::FAILURES_WINDOW_S,
        trust::FAILURE_SOURCES_MOST,
    );
    let mut exchange = FailureLimiter::default();
    let mut now = 0.0;
    let steps = vectors["steps"].as_array().unwrap();
    for step in steps {
        now += step["after"].as_f64().unwrap();
        let source = step["source"].as_str().unwrap();
        let budget = if step["kind"] == "identity" {
            &mut identity
        } else {
            &mut exchange
        };
        assert_eq!(
            budget.failure(source, now),
            step["retry"].as_u64().unwrap(),
            "{step}"
        );
    }

    let revokes = vectors["revokes"].as_array().unwrap();
    for step in revokes {
        let requested = step["requested"].as_str().unwrap();
        let own = (step["scope"] == "limited").then(|| step["own"].as_str().unwrap());
        let attempted = step["attempted"].as_array().unwrap();
        let expected = if let Some(first) = attempted.first() {
            Ok(first.as_str().unwrap())
        } else if step["status"] == 400 {
            Err(RevokeRefusal::Missing)
        } else {
            assert_eq!(step["status"], 403, "{step}");
            Err(RevokeRefusal::OtherDevice)
        };
        assert_eq!(devices::revoke_target(requested, own), expected, "{step}");
    }
    assert_eq!((steps.len(), revokes.len()), (106, 12));
}
