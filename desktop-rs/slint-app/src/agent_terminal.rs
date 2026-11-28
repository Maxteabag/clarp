//! `clarp-slint --agent-terminal <session> [--host <url>]`: an agent's
//! terminal on its Host, run in the system terminal without a window. The
//! desktop opens a terminal running this for a chat's terminal button.
//!
//! It finds the Host's device token as the app does (`CLARP_TOKEN` for the
//! Host the app was started on, the desktop keyring, the local admin token
//! for a Host on this machine), opens `GET /terminal/<session>`
//! (`clarp_net::terminal`), puts the terminal in raw mode, and carries keys
//! and output both ways, telling the Host the window's size at the start and
//! whenever it changes. When the terminal ends it says why in one line. When
//! the Host refuses it (a turn of the agent is running, say) its sentence
//! stays on screen until Enter is pressed.

use std::io::Write;

use clarp_core::settings::{Settings, normalized_base_url};
use clarp_net::terminal;

const USAGE: &str = "usage: clarp-slint --agent-terminal <session> [--host <host url>]";

/// The chat and Host after `--agent-terminal`, or why they are unusable.
/// Without `--host`, the Host the app uses.
pub fn arguments(args: &[String], settings: &Settings) -> Option<Result<(String, String), String>> {
    let at = args.iter().position(|a| a == "--agent-terminal")?;
    let Some(session) = args.get(at + 1).map(|s| s.trim().to_owned()).filter(|s| !s.is_empty() && !s.starts_with("--")) else {
        return Some(Err(USAGE.into()));
    };
    let host = match args.iter().position(|a| a == "--host") {
        Some(named) => match args.get(named + 1).filter(|url| !url.starts_with("--")) {
            Some(url) => normalized_base_url(url),
            None => return Some(Err(USAGE.into())),
        },
        None => primary_host(settings),
    };
    let valid = url::Url::parse(&host).is_ok_and(|u| matches!(u.scheme(), "http" | "https") && u.host_str().is_some_and(|h| !h.is_empty()));
    if !valid {
        return Some(Err(format!("{host} is not a Clarp server URL (https://host[:port])")));
    }
    Some(Ok((session, host)))
}

/// The Host the app connects to: `CLARP_BASE_URL`, else the saved one.
fn primary_host(settings: &Settings) -> String {
    let saved = settings.string("connection/baseUrl", "http://127.0.0.1:7682");
    normalized_base_url(&std::env::var("CLARP_BASE_URL").unwrap_or(saved))
}

/// The device token for `host`: `CLARP_TOKEN` only for the Host the app was
/// started on (it is never sent to another), then the keyring (with the
/// keyring off, as in checks, `CLARP_PEER_TOKEN` for every other Host), then
/// the local admin token for a Host on this machine.
async fn token_for(host: &str, settings: &Settings) -> String {
    let variable = |name: &str| std::env::var(name).ok().map(|value| value.trim().to_owned()).filter(|value| !value.is_empty());
    let primary = host == primary_host(settings);
    if primary && let Some(token) = variable("CLARP_TOKEN") {
        return token;
    }
    if std::env::var("CLARP_KEYRING").map_or(true, |v| v != "off") {
        let backend = clarp_net::credentials::Backend::from_settings(settings);
        let token = backend.lookup(host).await;
        if !token.trim().is_empty() {
            return token.trim().to_owned();
        }
    } else if !primary && let Some(token) = variable("CLARP_PEER_TOKEN") {
        return token;
    }
    clarp_core::settings::default_token(host)
}

/// Runs the terminal and returns the process's exit code.
pub fn run(parsed: Result<(String, String), String>, settings: Settings) -> i32 {
    // This process writes its own lines: in the app's language.
    clarp_core::i18n::set_current(clarp_core::i18n::resolve(&crate::language::stored(&settings)));
    let (session, host) = match parsed {
        Ok(parsed) => parsed,
        Err(message) => {
            eprintln!("clarp-slint: {message}");
            return 2;
        }
    };
    let runtime = match tokio::runtime::Builder::new_current_thread().enable_all().build() {
        Ok(runtime) => runtime,
        Err(error) => return hold(&format!("No async runtime: {error}"), None),
    };
    // Keys are read on a thread of their own: the same channel brings the
    // Enter that closes the window after a refusal.
    let mut keys = terminal::read_into_channel(std::io::stdin());
    // i18n: agent terminal window, first line while it connects; {0} is the agent's session name, {1} the Host's address
    println!("{}", clarp_core::tr!("Opening {0}'s terminal on {1}…", session, host));
    let opened = runtime.block_on(async {
        let token = token_for(&host, &settings).await;
        terminal::open(&host, &session, &token).await
    });
    let opened = match opened {
        Ok(opened) => opened,
        Err(sentence) => return hold(&sentence, Some(&mut keys)),
    };
    let ended = {
        let _raw = RawMode::enter();
        runtime.block_on(async {
            let (sender, sizes) = terminal::sizes();
            watch_size(sender);
            let mut output = tokio::io::stdout();
            terminal::carry(opened, &mut keys, &mut output, window_size(), sizes).await
        })
    };
    if ended.holds() {
        return hold(ended.line(), Some(&mut keys));
    }
    println!("\r\n{}", ended.line());
    0
}

/// Shows `sentence` and waits for Enter, so the window does not close
/// before it is read.
fn hold(sentence: &str, keys: Option<&mut tokio::sync::mpsc::UnboundedReceiver<Vec<u8>>>) -> i32 {
    let mut out = std::io::stdout();
    // i18n: agent terminal window, last line after it ended (Enter is the key)
    let _ = writeln!(out, "\r\n{sentence}\r\n{}", clarp_core::tr!("Press Enter to close."));
    let _ = out.flush();
    if let Some(keys) = keys {
        // What was typed before the sentence showed does not close it.
        while keys.try_recv().is_ok() {}
        while let Some(typed) = keys.blocking_recv() {
            if typed.iter().any(|b| matches!(b, b'\r' | b'\n')) {
                break;
            }
        }
    }
    1
}

/// The terminal's size (columns, rows), 80×24 when it has none.
fn window_size() -> (u16, u16) {
    // SAFETY: TIOCGWINSZ fills a winsize; a failure leaves it zeroed.
    let mut size: libc::winsize = unsafe { std::mem::zeroed() };
    let read = unsafe { libc::ioctl(libc::STDOUT_FILENO, libc::TIOCGWINSZ, &mut size) } == 0;
    if read && size.ws_col > 0 && size.ws_row > 0 { (size.ws_col, size.ws_row) } else { (80, 24) }
}

/// Sends the window's size each time it changes (SIGWINCH).
fn watch_size(sender: tokio::sync::mpsc::UnboundedSender<(u16, u16)>) {
    use tokio::signal::unix::{SignalKind, signal};
    let Ok(mut changed) = signal(SignalKind::window_change()) else { return };
    tokio::spawn(async move {
        while changed.recv().await.is_some() {
            if sender.send(window_size()).is_err() {
                break;
            }
        }
    });
}

/// The terminal in raw mode while it lives (keys go to the Host as typed,
/// Ctrl-C included); its settings come back on every way out.
struct RawMode(Option<libc::termios>);

impl RawMode {
    fn enter() -> Self {
        // SAFETY: tcgetattr fills the termios for a terminal; the raw copy
        // is applied only when that worked.
        unsafe {
            if libc::isatty(libc::STDIN_FILENO) != 1 {
                return RawMode(None);
            }
            let mut saved: libc::termios = std::mem::zeroed();
            if libc::tcgetattr(libc::STDIN_FILENO, &mut saved) != 0 {
                return RawMode(None);
            }
            let mut raw = saved;
            libc::cfmakeraw(&mut raw);
            if libc::tcsetattr(libc::STDIN_FILENO, libc::TCSANOW, &raw) != 0 {
                return RawMode(None);
            }
            RawMode(Some(saved))
        }
    }
}

impl Drop for RawMode {
    fn drop(&mut self) {
        if let Some(saved) = self.0.take() {
            // SAFETY: restores the settings read in `enter`.
            unsafe { libc::tcsetattr(libc::STDIN_FILENO, libc::TCSANOW, &saved) };
        }
    }
}

#[cfg(test)]
mod tests {
    use super::arguments;
    use clarp_core::settings::Settings;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn the_session_and_host_follow_the_flag() {
        let settings = Settings::in_memory();
        assert_eq!(arguments(&args(&["--headless"]), &settings), None, "not a terminal launch");
        assert_eq!(
            arguments(&args(&["--agent-terminal", "rachel", "--host", "https://host.example/"]), &settings),
            Some(Ok(("rachel".into(), "https://host.example".into())))
        );
        for list in [&["--agent-terminal"][..], &["--agent-terminal", "--host", "https://h"], &["--agent-terminal", "rachel", "--host"]] {
            assert!(matches!(arguments(&args(list), &settings), Some(Err(message)) if message.starts_with("usage:")), "{list:?}");
        }
        for url in ["ftp://host", "https://"] {
            assert!(matches!(arguments(&args(&["--agent-terminal", "r", "--host", url]), &settings), Some(Err(m)) if m.contains("not a Clarp server URL")), "{url}");
        }
    }
}
