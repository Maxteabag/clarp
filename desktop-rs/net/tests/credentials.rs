//! Device-token round trips. The Secret Service ones talk to whatever keyring
//! is on the session bus, so they only run under `net/tests/run-keyring-tests.sh`,
//! which starts a private bus with a throwaway gnome-keyring (or none at all).

use clarp_net::credentials;

fn mode() -> Option<String> {
    std::env::var("CLARP_TEST_SECRET_SERVICE").ok()
}

#[tokio::test]
async fn token_round_trips_through_an_isolated_keyring() {
    if mode().as_deref() != Some("isolated") {
        eprintln!("skipped: run net/tests/run-keyring-tests.sh");
        return;
    }
    let server = format!("https://credential-test.invalid/{}", std::process::id());
    assert_eq!(credentials::lookup(&server).await, "", "nothing stored yet");
    credentials::store(&server, "cld_first").await.expect("store");
    credentials::store(&server, "cld_test_native_desktop_credential").await.expect("replace");
    assert_eq!(credentials::lookup(&server).await, "cld_test_native_desktop_credential");
    assert_eq!(credentials::lookup("https://other.invalid").await, "", "scoped to the server");
    credentials::remove(&server).await.expect("remove");
    assert_eq!(credentials::lookup(&server).await, "");
    credentials::remove(&server).await.expect("removing nothing succeeds");
}

#[tokio::test]
async fn a_bus_without_a_keyring_fails_softly() {
    if mode().as_deref() != Some("absent") {
        eprintln!("skipped: run net/tests/run-keyring-tests.sh");
        return;
    }
    assert_eq!(credentials::lookup("https://nowhere.invalid").await, "");
    assert_eq!(
        credentials::store("https://nowhere.invalid", "cld_x").await,
        Err("Secret Service session could not be opened".into())
    );
    assert!(credentials::remove("https://nowhere.invalid").await.is_err());
}

/// The macOS Keychain round trip; CI sets `CLARP_TEST_KEYCHAIN=1` after
/// making a throwaway default keychain, so nobody's own login Keychain is
/// written.
#[cfg(target_os = "macos")]
#[tokio::test]
async fn token_round_trips_through_the_keychain() {
    if std::env::var("CLARP_TEST_KEYCHAIN").as_deref() != Ok("1") {
        eprintln!("skipped: set CLARP_TEST_KEYCHAIN=1 with a throwaway default keychain");
        return;
    }
    let server = format!("https://keychain-test.invalid/{}", std::process::id());
    assert_eq!(credentials::lookup(&server).await, "", "nothing stored yet");
    credentials::store(&server, "cld_first").await.expect("store");
    credentials::store(&server, "cld_test_native_desktop_credential").await.expect("replace");
    assert_eq!(credentials::lookup(&server).await, "cld_test_native_desktop_credential");
    assert_eq!(credentials::lookup("https://other.invalid").await, "", "scoped to the server");
    assert!(credentials::store(&server, "cld_with space").await.is_err(), "a token the tool cannot take is refused");
    assert_eq!(credentials::lookup(&server).await, "cld_test_native_desktop_credential", "and the stored one is kept");
    credentials::remove(&server).await.expect("remove");
    assert_eq!(credentials::lookup(&server).await, "");
    credentials::remove(&server).await.expect("removing nothing succeeds");
}
