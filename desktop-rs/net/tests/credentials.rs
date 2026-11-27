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

#[tokio::test]
async fn file_backend_round_trips_and_enforces_private_permissions() {
    let dir = std::env::temp_dir().join(format!("clarp-cred-file-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let path = dir.join("credentials.json");
    let backend = credentials::Backend::File { path: Some(path.clone()) };
    let server = "https://file-backend.invalid";

    assert_eq!(backend.lookup(server).await, "");
    backend.store(server, "cld_first").await.expect("store");
    backend.store(&format!("{server}/"), "cld_second").await.expect("replace normalized");
    assert_eq!(backend.lookup(server).await, "cld_second");
    assert_eq!(backend.lookup("https://other.invalid").await, "");

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(&path).expect("metadata").permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "credentials file must be 0600, got {:o}", mode);
    }

    backend.remove(server).await.expect("remove");
    assert_eq!(backend.lookup(server).await, "");
    backend.remove(server).await.expect("removing missing succeeds");

    let _ = std::fs::remove_dir_all(&dir);
}

#[cfg(unix)]
#[tokio::test]
async fn command_backend_round_trips_with_placeholders_env_and_stdin() {
    let dir = std::env::temp_dir().join(format!("clarp-cred-cmd-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let dir_str = dir.display().to_string();

    let backend = credentials::Backend::Command {
        lookup: format!("cat '{dir_str}/{{host}}.tok'"),
        store: Some(format!("cat > '{dir_str}/{{host}}.tok' && printf '%s' \"$CLARP_SERVER_URL\" > '{dir_str}/{{host}}.url'")),
        remove: Some(format!("rm -f '{dir_str}/{{host}}.tok' '{dir_str}/{{host}}.url'")),
    };
    let server = "https://cmd-host.example.test";

    assert!(backend.supports_store());
    assert!(!backend.needs_write_probe());
    assert_eq!(backend.lookup(server).await, "");
    backend.store(server, "cld_from_command").await.expect("store via command");
    assert_eq!(backend.lookup(server).await, "cld_from_command");
    assert_eq!(std::fs::read_to_string(dir.join("cmd-host.example.test.url")).unwrap(), server);
    backend.remove(server).await.expect("remove via command");
    assert_eq!(backend.lookup(server).await, "");

    let read_only = credentials::Backend::Command {
        lookup: "printf 'cld_readonly\\n'".into(),
        store: None,
        remove: None,
    };
    assert!(!read_only.supports_store());
    assert_eq!(read_only.lookup(server).await, "cld_readonly");
    assert!(read_only.store(server, "cld_x").await.is_err());
    assert!(read_only.remove(server).await.is_err());

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn backend_resolves_from_settings() {
    let mut settings = clarp_core::settings::Settings::in_memory();
    assert_eq!(credentials::Backend::from_settings(&settings), credentials::Backend::Keyring);

    clarp_core::prefs::set_text(&mut settings, "credentialsbackend", "file").unwrap();
    clarp_core::prefs::set_text(&mut settings, "credentialsfile", "/tmp/my-creds.json").unwrap();
    assert_eq!(
        credentials::Backend::from_settings(&settings),
        credentials::Backend::File { path: Some(std::path::PathBuf::from("/tmp/my-creds.json")) }
    );

    clarp_core::prefs::set_text(&mut settings, "credentialsbackend", "command").unwrap();
    clarp_core::prefs::set_text(&mut settings, "credentialscommand", "get-token {host}").unwrap();
    clarp_core::prefs::set_text(&mut settings, "credentialsstorecommand", "put-token {url}").unwrap();
    assert_eq!(
        credentials::Backend::from_settings(&settings),
        credentials::Backend::Command {
            lookup: "get-token {host}".into(),
            store: Some("put-token {url}".into()),
            remove: None,
        }
    );
}
