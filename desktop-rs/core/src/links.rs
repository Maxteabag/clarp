//! What the desktop may open for a chat (C++ `TerminalLaunch`,
//! `LocalReport.h` and `AppController::openAgentTerminal`): an agent's own CLI
//! in the default terminal, and local report files an agent wrote. Transcript
//! text is model, tool and web output, so each policy only ever widens to a
//! named, checked target.

use std::path::{Path, PathBuf};

use crate::protocol::Agent;

/// The agent's interactive CLI, resuming its native conversation.
pub fn native_terminal_launch(agent: &Agent) -> Result<(String, Vec<String>), String> {
    let conversation = &agent.conversation_id;
    if conversation.is_empty() || conversation.starts_with('-') || conversation.contains('\0') {
        return Err("Send a chat message first to create a native CLI session".into());
    }
    let (program, flag) = match agent.backend.as_str() {
        "claude" => ("claude", "--resume"),
        "codex" => ("codex", "resume"),
        "agy" => ("agy", "--conversation"),
        "grok" => ("grok", "--resume"),
        _ => return Err("This backend does not have a supported interactive CLI".into()),
    };
    Ok((program.into(), vec![flag.into(), conversation.clone()]))
}

/// How the default terminal is asked to run `program`: `xdg-terminal-exec`
/// with a working directory and title when installed, else
/// `x-terminal-emulator -e`. The Host token never leaks into the CLI.
pub fn terminal_command(
    xdg_terminal_exec: bool,
    session: &str,
    title: &str,
    directory: &str,
    program_path: &str,
    arguments: &[String],
) -> (String, Vec<String>) {
    let mut command: Vec<String> =
        vec!["env".into(), "-u".into(), "CLARP_TOKEN".into(), format!("CLAUDE_PWA_SESSION={session}"), program_path.into()];
    command.extend(arguments.iter().cloned());
    if xdg_terminal_exec {
        let mut launch = vec![format!("--dir={directory}"), format!("--title={title}"), "--".into()];
        launch.extend(command);
        ("xdg-terminal-exec".into(), launch)
    } else {
        command.insert(0, "-e".into());
        ("x-terminal-emulator".into(), command)
    }
}

/// An executable on `PATH` (C++ `QStandardPaths::findExecutable`).
pub fn find_executable(name: &str) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    std::env::var_os("PATH")?
        .to_str()?
        .split(':')
        .filter(|dir| !dir.is_empty())
        .map(|dir| Path::new(dir).join(name))
        .find(|path| std::fs::metadata(path).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0))
}

const REPORT_EXTENSIONS: &[&str] =
    &["html", "htm", "pdf", "txt", "md", "csv", "log", "json", "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"];

/// A local report an agent wrote, as a canonical path, when it is a
/// readable, non-executable HTML, PDF, text or image file (C++
/// `localReportUrl`). The explicit report action only; never a general
/// widening of which links open.
pub fn local_report_path(link: &str) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    let path = if link.starts_with('/') && !link.starts_with("//") {
        PathBuf::from(link)
    } else {
        let url = url::Url::parse(link).ok()?;
        if url.scheme() != "file" || url.host_str().is_some_and(|h| !h.is_empty()) || url.query().is_some() || url.fragment().is_some() {
            return None;
        }
        url.to_file_path().ok()?
    };
    let canonical = std::fs::canonicalize(path).ok()?;
    let metadata = std::fs::metadata(&canonical).ok()?;
    let mode = metadata.permissions().mode();
    // A privileged process may be allowed to read a mode-000 file: respect
    // the file's own read bits as well as access.
    if !metadata.is_file() || mode & 0o444 == 0 || mode & 0o111 != 0 {
        return None;
    }
    let extension = canonical.extension()?.to_str()?.to_lowercase();
    if !REPORT_EXTENSIONS.contains(&extension.as_str()) {
        return None;
    }
    let mut prefix = vec![0u8; 4096];
    let read = std::io::Read::read(&mut std::fs::File::open(&canonical).ok()?, &mut prefix).ok()?;
    prefix.truncate(read);
    let contains = |needle: &[u8]| prefix.windows(needle.len()).any(|w| w == needle);
    if prefix.starts_with(b"#!") || prefix.starts_with(b"\x7fELF") || prefix.starts_with(b"MZ") || contains(b"[Desktop Entry]") {
        return None;
    }
    // The content must look like what the name says (C++ matches the MIME
    // type by content): images and PDFs by magic, the rest as text.
    let looks_right = match extension.as_str() {
        "pdf" => prefix.starts_with(b"%PDF"),
        "png" => prefix.starts_with(b"\x89PNG"),
        "jpg" | "jpeg" => prefix.starts_with(b"\xff\xd8\xff"),
        "gif" => prefix.starts_with(b"GIF8"),
        "webp" => prefix.len() >= 12 && prefix.starts_with(b"RIFF") && &prefix[8..12] == b"WEBP",
        "bmp" => prefix.starts_with(b"BM"),
        _ => std::str::from_utf8(&prefix).is_ok() || std::str::from_utf8(&prefix[..prefix.len().saturating_sub(3)]).is_ok(),
    };
    looks_right.then_some(canonical)
}
