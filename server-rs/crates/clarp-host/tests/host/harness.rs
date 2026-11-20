//! The Host's tests of the harness itself: what the tests of every piece lean on. One file a
//! piece, declared in `host.rs`.

use super::*;

/// A wait that cannot come true fails in the words of the Host the test started: its start
/// line, and what it said after it, so a timeout says why the Host did not do what was waited for.
#[test]
fn a_wait_that_times_out_says_what_the_host_under_test_said() {
    let place = Place::new("harness-wait");
    let host = place.start();
    let made = host.post(
        "/agents",
        json!({ "name": "Wren", "session": "wren", "backend": "scripted" }),
    );
    assert_eq!(made["session"], "wren", "{made}");
    let failed = std::panic::catch_unwind(|| {
        wait_within("what cannot come", Duration::from_millis(200), || false);
    });
    let payload = failed.expect_err("a wait that cannot come true fails");
    let said = payload
        .downcast_ref::<String>()
        .cloned()
        .unwrap_or_default();
    assert!(
        said.starts_with("timed out waiting for what cannot come\n"),
        "{said}"
    );
    let start = format!("clarp-host listens on {}", host.address);
    assert!(
        said.contains(&format!("the Host that said \"{start}\"")),
        "{said}"
    );
    // A wait that comes true says nothing.
    wait_within("what is there", Duration::from_millis(200), || true);
}
