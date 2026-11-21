//! The Host's tests of its stand-ins: one rule for every address that stands in for a provider
//! or an engine (`clarp_kernel::address::on_this_machine`). One file a piece, declared in
//! `host.rs`.
//!
//! The addresses refused here are refused for what they are, not for what they would reach:
//! none names another host to a client than the host its text names.

use super::*;

/// Every variable that names a stand-in: whether it is reached by a socket (and not over the web),
/// and whether it may carry a path, and a query.
const STAND_INS: [(&str, bool, bool, bool); 19] = [
    ("CLARP_JUDGE_ENDPOINT", false, true, true),
    ("CLARP_SCRIBE_ENDPOINT", false, true, true),
    ("CLARP_DEEPGRAM_VOICE_ENDPOINT", false, true, true),
    ("CLARP_INK_ENDPOINT", false, true, true),
    ("CLARP_GOOGLE_VOICE_ENDPOINT", false, true, true),
    ("CLARP_ELEVEN_VOICE_ENDPOINT", false, true, true),
    ("CLARP_NOVA_ENDPOINT", false, true, true),
    ("CLARP_PICTURE_ENDPOINT", false, true, true),
    ("CLARP_ROUTER_ENDPOINT", false, true, true),
    ("CLARP_CARTESIA_ENDPOINT", false, true, true),
    ("CLARP_APNS_ENDPOINT", false, true, true),
    ("CLARP_FCM_ENDPOINT", false, false, true),
    ("CLARP_GEMINI_ENDPOINT", false, true, true),
    ("CLARP_GOOGLE_TOKEN_ENDPOINT", false, true, true),
    ("CLARP_GOOGLE_SPEECH_ENDPOINT", false, true, true),
    ("CLARP_FLUX_ENDPOINT", true, true, true),
    ("CLARP_LIVE_ENDPOINT", true, true, true),
    ("CLARP_GEMINI_LIVE_ENDPOINT", true, true, false),
    ("CLARP_RELAY_DIAL", true, true, true),
];

#[test]
fn a_host_given_a_stand_in_not_on_this_machine_refuses_to_start_naming_the_variable_and_not_the_address()
 {
    let place = Place::new("stand-ins");
    for (variable, socket, path, query) in STAND_INS {
        let scheme = if socket { "ws" } else { "http" };
        let other = if socket { "http" } else { "ws" };
        let mut refused = vec![
            // User information before a host that is this machine: refused all the same.
            format!("{scheme}://someone:a-secret-of-the-test@127.0.0.1:5000"),
            format!("{scheme}://example.invalid:5000"),
            format!("{scheme}://10.0.0.1:5000"),
            format!("{other}://127.0.0.1:5000"),
            format!("{scheme}://127.0.0.1"),
            format!("{scheme}://127.0.0.1:5000\\x"),
            format!("{scheme}://127.0.0.1:5000/a\\b"),
            // A path with a dot segment, as it was given (R139).
            format!("{scheme}://127.0.0.1:5000/a/../b"),
        ];
        if !path {
            refused.push(format!("{scheme}://127.0.0.1:5000/x"));
        }
        if !query {
            refused.push(format!("{scheme}://127.0.0.1:5000/x?y=1"));
        }
        for given in refused {
            // An engine of no name: a Host that took the address all the same ends there,
            // saying so, and is not left running.
            let started = Command::new(env!("CARGO_BIN_EXE_clarp-host"))
                .args(["--data", place.root.join("never").to_str().unwrap()])
                .args(["--engine", "of-no-name"])
                .env(variable, &given)
                .ended();
            let said = String::from_utf8_lossy(&started.stderr).into_owned();
            assert!(!started.status.success(), "{variable} {given}: it started");
            assert!(
                said.contains(&format!("{variable}: ")),
                "{variable}: {said}"
            );
            assert!(
                !said.contains("a-secret-of-the-test") && !said.contains("someone"),
                "{variable}: the address's user information was said: {said}"
            );
        }
    }
}

#[test]
fn a_host_given_its_stand_ins_on_this_machine_starts() {
    let place = Place::new("stand-ins-here");
    let world = [
        ("CLARP_SCRIBE_ENDPOINT", "http://localhost:5000/v1"),
        ("CLARP_NOVA_ENDPOINT", "http://[::1]:5000"),
        ("CLARP_FCM_ENDPOINT", "http://127.0.0.1:5001"),
        ("CLARP_LIVE_ENDPOINT", "ws://127.0.0.1:5002/live"),
    ];
    let host = place.start_in("", &[], &world);
    assert!(host.get("/server-info")["name"].is_string());
}
