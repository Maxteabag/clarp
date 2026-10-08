//! `clarp-slint --pair URL CODE`: pairs this desktop with a Host without a
//! window, so it can be set up over SSH. Exchanges the one-time code for a
//! device token, keeps the token in the desktop keyring (the Keychain on
//! macOS, the Secret Service on Linux) and the Host in the settings, says
//! what it did and exits; the next launch connects with them.
//! `CLARP_KEYRING=off` pairs without storing the token (checks).

use clarp_core::settings::normalized_base_url;

/// The Host URL and code after `--pair`, or why they are unusable.
pub fn arguments(args: &[String]) -> Option<Result<(String, String), String>> {
    let at = args.iter().position(|a| a == "--pair")?;
    let usage = "usage: clarp-slint --pair <host url> <one-time code>";
    let (Some(url), Some(code)) = (args.get(at + 1), args.get(at + 2)) else { return Some(Err(usage.into())) };
    let (url, code) = (normalized_base_url(url), code.trim().to_owned());
    if code.is_empty() || code.starts_with("--") {
        return Some(Err(usage.into()));
    }
    let valid = url::Url::parse(&url).is_ok_and(|u| matches!(u.scheme(), "http" | "https") && u.host_str().is_some_and(|h| !h.is_empty()));
    if !valid {
        return Some(Err(format!("{url} is not a Clarp server URL (https://host[:port])")));
    }
    Some(Ok((url, code)))
}

/// Pairs, prints the outcome, and returns the process's exit code.
pub fn run(parsed: Result<(String, String), String>, settings: clarp_core::settings::Settings) -> i32 {
    match pair(parsed, settings) {
        Ok(message) => {
            println!("{message}");
            0
        }
        Err(message) => {
            eprintln!("clarp-slint: pairing failed: {message}");
            1
        }
    }
}

fn device_name() -> String {
    let mut buffer = [0u8; 256];
    // SAFETY: the buffer is writable for its whole length.
    let named = unsafe { libc::gethostname(buffer.as_mut_ptr().cast(), buffer.len()) } == 0;
    let host = if named { String::from_utf8_lossy(&buffer[..buffer.iter().position(|b| *b == 0).unwrap_or(buffer.len())]).into_owned() } else { String::new() };
    let host = host.trim().trim_end_matches(".local");
    if host.is_empty() { "Clarp desktop".into() } else { format!("Clarp desktop on {host}") }
}

/// What to do when the keyring is locked, as over SSH on macOS.
const UNLOCK_HINT: &str = if cfg!(target_os = "macos") {
    "over SSH the login Keychain is locked to this session: run `security unlock-keychain ~/Library/Keychains/login.keychain-db` (it asks for the Mac's login password), or run --pair in Terminal on the Mac"
} else {
    "unlock the desktop keyring (log in to the desktop session)"
};

/// Stores and removes a throwaway item: what pairing will need.
async fn keyring_writable() -> Result<(), String> {
    let probe = format!("clarp-pair-probe://{}", std::process::id());
    let stored = clarp_net::credentials::store(&probe, "cld_probe").await;
    let removed = clarp_net::credentials::remove(&probe).await;
    stored?;
    removed.map_err(|error| format!("could not remove the probe item {probe}: {error}"))
}

fn pair(parsed: Result<(String, String), String>, mut settings: clarp_core::settings::Settings) -> Result<String, String> {
    let (url, code) = parsed?;
    let base = url::Url::parse(&url).map_err(|error| error.to_string())?;
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().map_err(|error| format!("no async runtime: {error}"))?;
    let keyring = std::env::var("CLARP_KEYRING").map_or(true, |v| v != "off");
    // The Host spends the code on the exchange: first make sure the token
    // can be kept, or the Host is left with a device nobody holds.
    if keyring {
        runtime.block_on(keyring_writable()).map_err(|error| {
            format!("{} cannot be written: {error}. The code was not used: {UNLOCK_HINT}, then pair again with the same code.", clarp_net::credentials::KEYRING_NAME)
        })?;
    }
    let token = runtime.block_on(clarp_net::pairing::exchange(&base, &code, &device_name()))?;
    let kept = if keyring {
        let stored = runtime.block_on(clarp_net::credentials::store(&url, &token));
        stored.map_err(|error| {
            format!("{url} paired, but the device token could not be kept in {}: {error}. The code is used up: revoke the new device on the Host, {UNLOCK_HINT}, and pair with a new code.", clarp_net::credentials::KEYRING_NAME)
        })?;
        if runtime.block_on(clarp_net::credentials::lookup(&url)) != token {
            return Err(format!("the device token for {url} did not read back from {}", clarp_net::credentials::KEYRING_NAME));
        }
        format!("the device token is in {}", clarp_net::credentials::KEYRING_NAME)
    } else {
        "the device token is not kept (CLARP_KEYRING=off)".into()
    };
    settings.set("connection/baseUrl", url.clone());
    let saved = match settings.path() {
        Some(path) => {
            let reread = clarp_core::settings::Settings::at(path);
            if reread.string("connection/baseUrl", "") != url {
                return Err(format!("could not save the Host in {}", path.display()));
            }
            format!("the Host is saved in {}", path.display())
        }
        None => "the Host is not saved (CLARP_SETTINGS=off)".into(),
    };
    Ok(format!("Paired with {url}: {kept}; {saved}."))
}

#[cfg(test)]
mod tests {
    use super::arguments;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn the_url_and_code_follow_the_flag() {
        assert_eq!(arguments(&args(&["--headless"])), None, "not a pairing launch");
        assert_eq!(
            arguments(&args(&["--pair", "https://laptopstudio.tailf14237.ts.net/", " 123456 "])),
            Some(Ok(("https://laptopstudio.tailf14237.ts.net".into(), "123456".into())))
        );
    }

    #[test]
    fn a_missing_or_unusable_argument_is_refused() {
        for list in [&["--pair"][..], &["--pair", "https://host"], &["--pair", "https://host", "--headless"], &["--pair", "https://host", "  "]] {
            assert!(matches!(arguments(&args(list)), Some(Err(message)) if message.starts_with("usage:")), "{list:?}");
        }
        for url in ["laptopstudio", "ftp://host", "https://"] {
            assert!(matches!(arguments(&args(&["--pair", url, "123456"])), Some(Err(message)) if message.contains("not a Clarp server URL")), "{url}");
        }
    }
}
