//! One process per desktop (C++ `InstanceServer`): a second launch of the
//! same Clarp (same binary build, instance, Host, config and display) opens
//! a window in the running process instead of starting another. The
//! launching process only forwards its arguments over a Unix socket and
//! exits.
//!
//! The wire format is the C++ one: a 4-byte big-endian length, then
//! NUL-terminated UTF-8 arguments; the server answers `ok`.

use std::io::{Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::time::Duration;

use sha2::{Digest, Sha256};

pub const REPLY_TIMEOUT: Duration = Duration::from_secs(2);
pub const MAX_MESSAGE_BYTES: usize = 64 * 1024;

/// What makes two launches the same desktop, besides the binary.
pub const IDENTITY_VARIABLES: &[&str] = &[
    "CLARP_INSTANCE_NAME",
    "CLARP_BASE_URL",
    "CLARP_TOKEN",
    "CLARP_SHARED_FILESYSTEM_HOST",
    "XDG_CONFIG_HOME",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "QT_QPA_PLATFORM",
    "CLARP_RENDERER",
    "QT_QUICK_BACKEND",
];

const SEPARATE_FLAGS: &[&str] = &["-h", "--help", "--help-all", "-v", "--version", "--preview-versions"];

/// This launch runs on its own: help, version and the version manager,
/// screenshot runs, update relaunches, QML probes, CLARP_SEPARATE_PROCESS.
pub fn runs_alone(arguments: &[String], env: impl Fn(&str) -> Option<String>) -> bool {
    arguments.iter().any(|argument| SEPARATE_FLAGS.contains(&argument.as_str()))
        || env("CLARP_SEPARATE_PROCESS").is_some()
        || env("CLARP_SCREENSHOT_PATH").is_some()
        || env("CLARP_RS_QML").is_some()
        || env("CLARP_RESTORE_DESKTOP").as_deref() == Some("1")
}

/// A newly installed binary must not open its windows in an older process,
/// so the build (path and modification time) is part of the key.
pub fn executable_identity(executable: &Path) -> Option<Vec<u8>> {
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::MetadataExt;
    let metadata = std::fs::metadata(executable).ok()?;
    let mut identity = executable.as_os_str().as_bytes().to_vec();
    identity.push(0);
    identity.extend(format!("{}.{}", metadata.mtime(), metadata.mtime_nsec()).bytes());
    Some(identity)
}

/// The socket this launch would share, or `None` when it must run alone
/// (C++ `instanceSocketPath`).
pub fn socket_path(arguments: &[String], executable: &Path, env: impl Fn(&str) -> Option<String>) -> Option<PathBuf> {
    let runtime = env("XDG_RUNTIME_DIR").filter(|dir| !dir.is_empty())?;
    if runs_alone(arguments, &env) {
        return None;
    }
    let mut identity = executable_identity(executable)?;
    for name in IDENTITY_VARIABLES {
        identity.push(0);
        identity.extend(format!("{name}={}", env(name).unwrap_or_default()).bytes());
    }
    let digest: String = Sha256::digest(&identity).iter().map(|byte| format!("{byte:02x}")).collect();
    Some(Path::new(&runtime).join(format!("clarp-desktop-{}.sock", &digest[..20])))
}

pub fn encode(arguments: &[String]) -> Vec<u8> {
    let body: Vec<u8> = arguments.iter().flat_map(|argument| argument.bytes().chain([0])).collect();
    let mut message = (body.len() as u32).to_be_bytes().to_vec();
    message.extend(body);
    message
}

/// The arguments once `buffer` holds a whole message; `Err` for a message
/// larger than the server takes.
pub fn decode(buffer: &[u8]) -> Result<Option<Vec<String>>, String> {
    let Some(header) = buffer.get(..4) else { return Ok(None) };
    let length = u32::from_be_bytes(header.try_into().expect("four bytes")) as usize;
    if length > MAX_MESSAGE_BYTES {
        return Err(format!("a {length}-byte launch message is over the limit"));
    }
    let Some(body) = buffer.get(4..4 + length) else { return Ok(None) };
    Ok(Some(
        body.split(|byte| *byte == 0).filter(|part| !part.is_empty()).map(|part| String::from_utf8_lossy(part).into_owned()).collect(),
    ))
}

/// Hands `arguments` to a running instance; true only when it accepted
/// them (C++ `forwardToRunningInstance`).
pub fn forward(socket: &Path, arguments: &[String]) -> bool {
    let Ok(mut stream) = UnixStream::connect(socket) else { return false };
    // No half-close: the C++ server treats it as a disconnect and could not
    // write its reply back.
    if stream.write_all(&encode(arguments)).is_err() || stream.set_read_timeout(Some(REPLY_TIMEOUT)).is_err() {
        return false;
    }
    let mut reply = [0u8; 8];
    matches!(stream.read(&mut reply), Ok(read) if read >= 2 && &reply[..2] == b"ok")
}

/// Listens on `socket` and calls `requested` with each forwarded launch,
/// from the accepting thread. Reaching here means no live instance
/// answered, so a socket file left there belongs to a process that is gone.
pub fn listen(socket: &Path, requested: impl Fn(Vec<String>) + Send + 'static) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    match std::fs::remove_file(socket) {
        Err(error) if error.kind() != std::io::ErrorKind::NotFound => return Err(error),
        _ => {}
    }
    let listener = UnixListener::bind(socket)?;
    // Only this user may open windows here.
    std::fs::set_permissions(socket, std::fs::Permissions::from_mode(0o700))?;
    let path = socket.to_owned();
    std::thread::Builder::new().name("instance-server".into()).spawn(move || {
        for client in listener.incoming() {
            match client {
                Ok(client) => match serve(client) {
                    Ok(arguments) => requested(arguments),
                    Err(error) => eprintln!("InstanceServer: dropped a launch on {}: {error}", path.display()),
                },
                Err(error) => eprintln!("InstanceServer: accept failed on {}: {error}", path.display()),
            }
        }
    })?;
    Ok(())
}

fn serve(mut client: UnixStream) -> Result<Vec<String>, String> {
    client.set_read_timeout(Some(REPLY_TIMEOUT)).map_err(|e| e.to_string())?;
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        if let Some(arguments) = decode(&buffer)? {
            client.write_all(b"ok").map_err(|e| e.to_string())?;
            return Ok(arguments);
        }
        match client.read(&mut chunk) {
            Ok(0) => return Err("the launcher hung up mid-message".into()),
            Ok(read) => buffer.extend_from_slice(&chunk[..read]),
            Err(error) => return Err(error.to_string()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env(values: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = values.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
        move |name| map.get(name).cloned()
    }

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|v| v.to_string()).collect()
    }

    #[test]
    fn socket_is_keyed_by_build_host_and_display() {
        let exe = std::env::current_exe().unwrap();
        let base = [("XDG_RUNTIME_DIR", "/run/user/1"), ("CLARP_BASE_URL", "http://a"), ("WAYLAND_DISPLAY", "wayland-1")];
        let one = socket_path(&[], &exe, env(&base)).unwrap();
        assert!(one.starts_with("/run/user/1"));
        assert!(one.file_name().unwrap().to_str().unwrap().starts_with("clarp-desktop-"));
        assert_eq!(one, socket_path(&args(&["--backend", "codex"]), &exe, env(&base)).unwrap(), "arguments are forwarded, not keyed");
        let other_host = [base[0], ("CLARP_BASE_URL", "http://b"), base[2]];
        assert_ne!(one, socket_path(&[], &exe, env(&other_host)).unwrap());
        let other_display = [base[0], base[1], ("WAYLAND_DISPLAY", "wayland-2")];
        assert_ne!(one, socket_path(&[], &exe, env(&other_display)).unwrap());
    }

    #[test]
    fn some_launches_always_run_alone() {
        let exe = std::env::current_exe().unwrap();
        let runtime = ("XDG_RUNTIME_DIR", "/run/user/1");
        assert!(socket_path(&[], &exe, env(&[])).is_none(), "no runtime dir");
        for flag in ["--help", "-v", "--preview-versions"] {
            assert!(socket_path(&args(&[flag]), &exe, env(&[runtime])).is_none(), "{flag}");
        }
        for variable in [("CLARP_SEPARATE_PROCESS", "1"), ("CLARP_SCREENSHOT_PATH", "/x.png"), ("CLARP_RESTORE_DESKTOP", "1"), ("CLARP_RS_QML", "p.qml")] {
            assert!(socket_path(&[], &exe, env(&[runtime, variable])).is_none(), "{variable:?}");
        }
        assert!(socket_path(&[], &exe, env(&[runtime, ("CLARP_RESTORE_DESKTOP", "0")])).is_some());
    }

    #[test]
    fn message_round_trips_and_waits_for_the_whole_body() {
        let message = encode(&args(&["--cwd", "/tmp/a b", "--backend", "grok"]));
        assert_eq!(&message[..4], &(message.len() as u32 - 4).to_be_bytes());
        assert_eq!(decode(&message[..3]), Ok(None));
        assert_eq!(decode(&message[..message.len() - 1]), Ok(None));
        assert_eq!(decode(&message).unwrap().unwrap(), args(&["--cwd", "/tmp/a b", "--backend", "grok"]));
        assert_eq!(decode(&encode(&[])).unwrap().unwrap(), Vec::<String>::new());
        let mut huge = ((MAX_MESSAGE_BYTES + 1) as u32).to_be_bytes().to_vec();
        huge.push(0);
        assert!(decode(&huge).is_err());
    }

    #[test]
    fn a_second_launch_is_forwarded_to_the_listener() {
        let dir = std::env::temp_dir().join(format!("clarp-instance-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let socket = dir.join("s.sock");
        assert!(!forward(&socket, &args(&["--new-agent"])), "nobody listening");
        // A stale socket file from a dead process is replaced.
        drop(UnixListener::bind(&socket).unwrap());
        let (sender, received) = std::sync::mpsc::channel();
        listen(&socket, move |arguments| sender.send(arguments).unwrap()).unwrap();
        assert!(forward(&socket, &args(&["--backend", "codex", "--cwd", "/w"])));
        assert_eq!(received.recv_timeout(Duration::from_secs(2)).unwrap(), args(&["--backend", "codex", "--cwd", "/w"]));
        // An oversized message is refused, not answered.
        let mut stream = UnixStream::connect(&socket).unwrap();
        stream.write_all(&((MAX_MESSAGE_BYTES + 1) as u32).to_be_bytes()).unwrap();
        let mut reply = Vec::new();
        stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        assert_eq!(stream.read_to_end(&mut reply).unwrap(), 0);
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&socket).unwrap().permissions().mode() & 0o777, 0o700);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
