//! Handing URLs and files to the desktop's opener.
//!
//! The opener is waited for on a thread of its own, so it never lingers as
//! a zombie of the app, and the GUI thread never waits on it.
//!
//! An HTML artifact's page opens in a new browser window, not a tab of the
//! browser's last window: that window may be on another workspace, and a
//! compositor that follows activation (Hyprland's `misc:focus_on_activate`)
//! switches there, which takes Clarp out of view as if it had closed. A new
//! window maps on the current workspace, beside Clarp.

use std::process::Command;

/// Opens `target` (a URL or a path); `own_window` opens a web page in a
/// browser window of its own when the default browser is one we know.
pub fn open(target: &str, own_window: bool) {
    let target = target.to_owned();
    let spawned = std::thread::Builder::new().name("opener".into()).spawn(move || {
        let window = if own_window { browser_window(&target) } else { None };
        let mut command = window.unwrap_or_else(|| {
            let mut command = Command::new(super::OPENER);
            command.arg(&target);
            command
        });
        if own_window {
            eprintln!("clarp-slint: opening {target} with {}", command.get_program().to_string_lossy());
        }
        match command.spawn() {
            Ok(mut child) => match child.wait() {
                Ok(status) if !status.success() => eprintln!("clarp-slint: the opener of {target} exited with {status}"),
                Ok(_) => {}
                Err(error) => eprintln!("clarp-slint: the opener of {target}: {error}"),
            },
            Err(error) => eprintln!("clarp-slint: could not open {target}: {error}"),
        }
    });
    if let Err(error) = spawned {
        eprintln!("clarp-slint: could not open a page: {error}");
    }
}

/// The default browser asked for a new window, or None to use the opener.
#[cfg(target_os = "linux")]
fn browser_window(url: &str) -> Option<Command> {
    let output = match Command::new("xdg-settings").args(["get", "default-web-browser"]).output() {
        Ok(output) if output.status.success() => output,
        Ok(output) => {
            eprintln!("clarp-slint: no default browser ({}); opening in a tab", output.status);
            return None;
        }
        Err(error) => {
            eprintln!("clarp-slint: no default browser ({error}); opening in a tab");
            return None;
        }
    };
    let entry = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    let Some(exec) = desktop_exec(&entry) else {
        eprintln!("clarp-slint: no launcher for the browser {entry:?}; opening in a tab");
        return None;
    };
    let Some((program, args)) = new_window(&exec, url) else {
        eprintln!("clarp-slint: the browser {entry:?} has no known new-window option; opening in a tab");
        return None;
    };
    let mut command = Command::new(program);
    command.args(args);
    Some(command)
}

#[cfg(not(target_os = "linux"))]
fn browser_window(_url: &str) -> Option<Command> {
    None
}

/// The Exec line of the desktop entry `entry` in the XDG data folders.
#[cfg(target_os = "linux")]
fn desktop_exec(entry: &str) -> Option<String> {
    if !entry.ends_with(".desktop") || entry.contains('/') {
        return None;
    }
    let home = std::env::var_os("XDG_DATA_HOME").filter(|d| !d.is_empty()).map(std::path::PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| std::path::Path::new(&h).join(".local/share")));
    let shared = std::env::var("XDG_DATA_DIRS").ok().filter(|d| !d.is_empty()).unwrap_or_else(|| "/usr/local/share:/usr/share".into());
    home.into_iter()
        .chain(shared.split(':').map(std::path::PathBuf::from))
        .find_map(|dir| std::fs::read_to_string(dir.join("applications").join(entry)).ok())
        .and_then(|text| exec_line(&text))
}

/// The `[Desktop Entry]` section's Exec value.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn exec_line(desktop: &str) -> Option<String> {
    let mut main = false;
    for line in desktop.lines().map(str::trim) {
        if line.starts_with('[') {
            main = line == "[Desktop Entry]";
        } else if main && let Some(exec) = line.strip_prefix("Exec=") {
            return Some(exec.trim().to_owned());
        }
    }
    None
}

/// The program and arguments that open `url` in a new window of the
/// browser an Exec line starts, keeping its own options (Omarchy's Wayland
/// flags) but not its field codes (%U). None for a browser we do not know,
/// or a line we cannot split safely (quoted, or through env or flatpak).
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn new_window(exec: &str, url: &str) -> Option<(String, Vec<String>)> {
    const KNOWN: &[&str] = &["google-chrome", "chromium", "brave", "microsoft-edge", "vivaldi", "firefox", "librewolf", "zen-browser"];
    if exec.contains(['"', '\'', '\\']) {
        return None;
    }
    let mut words = exec.split_whitespace();
    let program = words.next()?;
    let name = std::path::Path::new(program).file_name()?.to_str()?;
    if !KNOWN.iter().any(|known| name.starts_with(known)) {
        return None;
    }
    let mut args: Vec<String> = words.filter(|w| !w.starts_with('%') && *w != "--new-window").map(str::to_owned).collect();
    args.push("--new-window".into());
    args.push(url.to_owned());
    Some((program.to_owned(), args))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_known_browser_opens_a_page_in_a_new_window_with_its_own_options() {
        let url = "http://127.0.0.1:4242/form/abc";
        assert_eq!(
            new_window("/usr/bin/google-chrome-stable --ozone-platform=wayland %U", url),
            Some(("/usr/bin/google-chrome-stable".into(), vec!["--ozone-platform=wayland".into(), "--new-window".into(), url.into()]))
        );
        assert_eq!(new_window("firefox %u", url), Some(("firefox".into(), vec!["--new-window".into(), url.into()])));
        for unknown in ["epiphany %U", "env MOZ=1 firefox %u", "flatpak run org.chromium.Chromium", "\"/opt/my chrome/chrome\" %U", ""] {
            assert_eq!(new_window(unknown, url), None, "{unknown}");
        }
    }

    #[test]
    fn the_exec_line_is_the_main_sections() {
        let entry = "[Desktop Entry]\nName=Chrome\nExec=/usr/bin/google-chrome-stable %U\n[Desktop Action new-window]\nExec=/usr/bin/google-chrome-stable --incognito\n";
        assert_eq!(exec_line(entry).as_deref(), Some("/usr/bin/google-chrome-stable %U"));
        assert_eq!(exec_line("[Desktop Action x]\nExec=evil\n"), None);
    }
}
