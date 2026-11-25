//! Who still uses the administrator's token, one line each (#795).
//!
//! The administrator's token is the owner's. An agent's tools are meant to present the token of
//! the agents' tools (`tool_token`), and until every caller does, the Host names the ones that
//! do not: every request that arrives with the administrator's token in its header, from no
//! app (`X-Clarp-Client`), with no turn (`X-Clarp-Turn`, or a turn in a tool call's own
//! environment), and without saying it is the owner's own command ([`crate::OWNER_MODE_HEADER`])
//! gets one line, `clarp-host: admin token without a turn:`; a week of them names every caller
//! still to be moved. A call in the owner's mode gets one line too, `clarp-host: owner mode:`,
//! so an agent that slips into it is seen. Neither line ever holds a token, nor what a call
//! carries: the route or the tool and its verb, and the process that asked (its id, its
//! parent's, its directory and its program), where the machine says.
//!
//! The process of a request that is not a tool call is found by its connection: the socket this
//! machine's kernel lists with the caller's address and port, and the process that holds it.
//! Only a caller on this machine is looked for, and only the user's own processes can be read.
//! That reads every process's open files, so a route's caller is looked for once a minute; a
//! line between says so.

use std::net::SocketAddr;
use std::path::Path;

use axum::http::HeaderMap;

/// How a request with the administrator's token stands, as these lines see it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Seen {
    /// The owner's own command, said so.
    OwnerMode,
    /// The administrator's token, by no app, with no turn and no word that it is the owner's:
    /// a caller still to be moved to the tools' token.
    Unmoved,
    /// Anything else: an app, a turn, a cookie or an address (a browser), an alias.
    Other,
}

/// How a request that the administrator's token let in stands. `primary_in_header`: it is the
/// Host's own token (not an alias) and it came in the `Authorization` header.
pub(crate) fn seen(headers: &HeaderMap, primary_in_header: bool) -> Seen {
    let said = |name: &str| {
        headers
            .get(name)
            .is_some_and(|value| !value.as_bytes().iter().all(u8::is_ascii_whitespace))
    };
    if said(crate::OWNER_MODE_HEADER) {
        return Seen::OwnerMode;
    }
    if primary_in_header && !said("x-clarp-client") && !said(crate::turn_identity::HEADER) {
        return Seen::Unmoved;
    }
    Seen::Other
}

/// The line for a request to a route, where it is one to write; the caller's process found by
/// its connection. Never a token: only the method, the route and what the machine says of the
/// process.
pub(crate) fn route_line(
    seen: Seen,
    method: &str,
    path: &str,
    peer: Option<SocketAddr>,
    host_port: u16,
) -> Option<String> {
    let head = head(seen)?;
    let route = format!("{method} {}", clean(path, 120));
    // Finding a process reads every process's open files: it is done for a route once a
    // minute, and a line between says so. Every call still has its line.
    let process = if looked_up_lately(&route, std::time::Instant::now()) {
        "process as named for this route within the minute".to_string()
    } else {
        peer.filter(|peer| peer.ip().is_loopback())
            .and_then(|peer| process_of(Path::new("/proc"), peer, host_port))
            .map_or_else(|| "process unknown".to_string(), |found| found.said())
    };
    Some(format!("{head} {route} ({process})"))
}

/// How long a route's process, once looked for, is not looked for again.
const LOOKED_UP_FOR: std::time::Duration = std::time::Duration::from_secs(60);

/// Whether the process of a call to `route` was looked for within [`LOOKED_UP_FOR`]; where it
/// was not, it is to be looked for now, and that is noted.
fn looked_up_lately(route: &str, now: std::time::Instant) -> bool {
    use std::collections::HashMap;
    use std::sync::{Mutex, OnceLock};
    static LOOKED: OnceLock<Mutex<HashMap<String, std::time::Instant>>> = OnceLock::new();
    let mut looked = LOOKED
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    looked.retain(|_, at| now.saturating_duration_since(*at) < LOOKED_UP_FOR);
    if looked.contains_key(route) {
        return true;
    }
    looked.insert(route.to_string(), now);
    false
}

/// The line for a tool call, where it is one to write: the tool and its verb, and the process
/// that called it as the tool says (its parent and its directory).
pub(crate) fn tool_line(
    seen: Seen,
    call: &crate::tools::ToolCall,
    turn_in_call: bool,
) -> Option<String> {
    let seen = if seen == Seen::Unmoved && turn_in_call {
        Seen::Other
    } else {
        seen
    };
    let head = head(seen)?;
    Some(format!(
        "{head} tool {} {} (ppid {}, cwd {})",
        clean(&call.tool, 60),
        verb(&call.argv),
        call.ppid,
        clean(&call.cwd, 160)
    ))
}

fn head(seen: Seen) -> Option<&'static str> {
    match seen {
        Seen::OwnerMode => Some("clarp-host: owner mode:"),
        Seen::Unmoved => Some("clarp-host: admin token without a turn:"),
        Seen::Other => None,
    }
}

/// A tool's verb: its first words up to the first option, two at most (`agent create`,
/// `prompt`, `job-heartbeat`), each a plain word. What follows may be a message, and is not
/// written.
pub(crate) fn verb(argv: &[String]) -> String {
    let plain = |word: &&String| {
        !word.is_empty()
            && word.len() <= 40
            && word
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    };
    let words: Vec<&str> = argv
        .iter()
        .take_while(|word| !word.starts_with('-'))
        .take(2)
        .take_while(plain)
        .map(String::as_str)
        .collect();
    if words.is_empty() {
        "-".into()
    } else {
        words.join(" ")
    }
}

/// A text as a line may hold it: one line, printable, cut at `most` characters.
fn clean(text: &str, most: usize) -> String {
    let mut said: String = text
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .take(most)
        .collect();
    if text.chars().count() > most {
        said.push('…');
    }
    said
}

/// A process the machine names.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Process {
    pub pid: u32,
    pub ppid: u32,
    pub cwd: String,
    pub program: String,
}

impl Process {
    fn said(&self) -> String {
        format!(
            "pid {}, ppid {}, cwd {}, {}",
            self.pid,
            self.ppid,
            clean(&self.cwd, 160),
            clean(&self.program, 160)
        )
    }
}

/// The process that holds the connection whose own end is `peer` and whose other end is this
/// Host's port, as `proc` (the kernel's `/proc`) lists them. `None` where none is found.
pub(crate) fn process_of(proc: &Path, peer: SocketAddr, host_port: u16) -> Option<Process> {
    let inode = ["net/tcp", "net/tcp6"].iter().find_map(|table| {
        socket_inode(
            &std::fs::read_to_string(proc.join(table)).ok()?,
            peer,
            host_port,
        )
    })?;
    let wanted = format!("socket:[{inode}]");
    for entry in std::fs::read_dir(proc).ok()?.flatten() {
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<u32>().ok())
        else {
            continue;
        };
        let Ok(fds) = std::fs::read_dir(entry.path().join("fd")) else {
            continue;
        };
        let holds = fds.flatten().any(|fd| {
            std::fs::read_link(fd.path()).is_ok_and(|link| link.as_os_str() == wanted.as_str())
        });
        if holds {
            return Some(described(&entry.path(), pid));
        }
    }
    None
}

fn described(dir: &Path, pid: u32) -> Process {
    let status = std::fs::read_to_string(dir.join("status")).unwrap_or_default();
    let ppid = status
        .lines()
        .find_map(|line| line.strip_prefix("PPid:"))
        .and_then(|value| value.trim().parse().ok())
        .unwrap_or(0);
    let cwd = std::fs::read_link(dir.join("cwd"))
        .map(|cwd| cwd.display().to_string())
        .unwrap_or_default();
    // The program and its first argument (a script's name), never what follows: an argument
    // may be a message, or a token.
    let cmdline = std::fs::read(dir.join("cmdline")).unwrap_or_default();
    let program: Vec<String> = cmdline
        .split(|b| *b == 0)
        .filter(|part| !part.is_empty())
        .take(2)
        .map(|part| String::from_utf8_lossy(part).into_owned())
        .collect();
    Process {
        pid,
        ppid,
        cwd,
        program: program.join(" "),
    }
}

/// The inode of the socket in a table of the kernel's (`/proc/net/tcp` or `tcp6`) whose own
/// end is `peer` and whose other end's port is `host_port`.
fn socket_inode(table: &str, peer: SocketAddr, host_port: u16) -> Option<String> {
    table.lines().skip(1).find_map(|line| {
        let fields: Vec<&str> = line.split_whitespace().collect();
        let (local, remote, inode) = (fields.get(1)?, fields.get(2)?, fields.get(9)?);
        let (local_ip, local_port) = local.split_once(':')?;
        let (_, remote_port) = remote.split_once(':')?;
        let ours = u16::from_str_radix(local_port, 16).ok()? == peer.port()
            && u16::from_str_radix(remote_port, 16).ok()? == host_port
            && same_ip(local_ip, peer);
        (ours && *inode != "0").then(|| (*inode).to_string())
    })
}

/// Whether an address as the kernel's table writes it (the bytes of each 32-bit word in the
/// machine's order, in hex) is `peer`'s.
fn same_ip(hex: &str, peer: SocketAddr) -> bool {
    let bytes: Option<Vec<u8>> = (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(hex.get(i..i + 2)?, 16).ok())
        .collect();
    let Some(bytes) = bytes else {
        return false;
    };
    // Each word of four bytes is written in the machine's order.
    let mut ordered = Vec::with_capacity(bytes.len());
    for word in bytes.chunks(4) {
        let mut word = word.to_vec();
        if cfg!(target_endian = "little") {
            word.reverse();
        }
        ordered.extend(word);
    }
    match peer {
        SocketAddr::V4(v4) => match ordered.len() {
            4 => ordered == v4.ip().octets(),
            16 => ordered == v4.ip().to_ipv6_mapped().octets(),
            _ => false,
        },
        SocketAddr::V6(v6) => ordered.len() == 16 && ordered == v6.ip().octets(),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    fn headers(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
        let mut headers = HeaderMap::new();
        for (name, value) in pairs {
            headers.insert(*name, HeaderValue::from_static(value));
        }
        headers
    }

    #[test]
    fn the_admin_token_without_app_turn_or_owner_mode_is_a_caller_to_move() {
        assert_eq!(seen(&headers(&[]), true), Seen::Unmoved);
        assert_eq!(
            seen(&headers(&[("x-clarp-owner-mode", "1")]), true),
            Seen::OwnerMode
        );
        // An alias, or a token in a cookie or an address, is not counted.
        assert_eq!(seen(&headers(&[]), false), Seen::Other);
        assert_eq!(
            seen(
                &headers(&[("x-clarp-client", "desktop/1 contract=1")]),
                true
            ),
            Seen::Other
        );
        assert_eq!(
            seen(&headers(&[("x-clarp-turn", "1.ab.2.cd")]), true),
            Seen::Other
        );
        // A blank header says nothing.
        assert_eq!(
            seen(&headers(&[("x-clarp-owner-mode", " ")]), true),
            Seen::Unmoved
        );
    }

    #[test]
    fn a_tool_line_names_the_tool_its_verb_and_its_process_and_never_what_it_says() {
        let call = crate::tools::ToolCall {
            tool: "clarp-admin".into(),
            argv: vec![
                "prompt".into(),
                "--to".into(),
                "x".into(),
                "--text".into(),
                "a secret".into(),
            ],
            cwd: "/home/u/work".into(),
            ppid: 42,
            ..crate::tools::ToolCall::default()
        };
        let line = tool_line(Seen::Unmoved, &call, false).unwrap();
        assert_eq!(
            line,
            "clarp-host: admin token without a turn: tool clarp-admin prompt (ppid 42, cwd /home/u/work)"
        );
        assert!(!line.contains("secret"));
        // A call that carries its turn is no caller to move; the owner's mode is written always.
        assert_eq!(tool_line(Seen::Unmoved, &call, true), None);
        assert!(
            tool_line(Seen::OwnerMode, &call, true)
                .unwrap()
                .starts_with("clarp-host: owner mode:")
        );
        assert_eq!(tool_line(Seen::Other, &call, false), None);
    }

    #[test]
    fn a_routes_process_is_looked_for_once_a_minute() {
        let start = std::time::Instant::now();
        let route = format!("GET /test-route-{}", crate::ids::hex(6));
        assert!(!looked_up_lately(&route, start));
        assert!(looked_up_lately(
            &route,
            start + std::time::Duration::from_secs(59)
        ));
        assert!(!looked_up_lately(
            &route,
            start + std::time::Duration::from_secs(61)
        ));
    }

    #[test]
    fn a_verb_is_at_most_two_plain_words_before_the_first_option() {
        let words = |said: &[&str]| verb(&said.iter().map(|w| w.to_string()).collect::<Vec<_>>());
        assert_eq!(
            words(&["agent", "create", "Theo", "--parent", "p"]),
            "agent create"
        );
        assert_eq!(words(&["s", "job-heartbeat", "bg1:x"]), "s job-heartbeat");
        assert_eq!(words(&["--session", "x"]), "-");
        assert_eq!(words(&["tell-user", "hello there"]), "tell-user");
        assert_eq!(words(&[]), "-");
    }

    #[test]
    fn the_process_of_a_connection_is_found_by_its_socket() {
        let dir = std::env::temp_dir().join(format!("clarp-owner-log-{}", crate::ids::hex(6)));
        let root = dir.as_path();
        std::fs::create_dir_all(root.join("net")).unwrap();
        // 127.0.0.1:40000 -> 127.0.0.1:7690, inode 777; a listening socket, and another's.
        let tcp = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n\
           0: 0100007F:1E0A 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 111 1\n\
           1: 0100007F:9C40 0100007F:1E0A 01 00000000:00000000 00:00000000 00000000  1000        0 777 1\n\
           2: 0100007F:9C41 0100007F:1E0A 01 00000000:00000000 00:00000000 00000000  1000        0 778 1\n";
        std::fs::write(root.join("net/tcp"), tcp).unwrap();
        for (pid, inode) in [(10_u32, 778_u32), (20, 777)] {
            let dir = root.join(pid.to_string());
            std::fs::create_dir_all(dir.join("fd")).unwrap();
            std::os::unix::fs::symlink(format!("socket:[{inode}]"), dir.join("fd/3")).unwrap();
            std::os::unix::fs::symlink("/work/here", dir.join("cwd")).unwrap();
            std::fs::write(dir.join("status"), "Name:\tpython3\nPPid:\t5\n").unwrap();
            std::fs::write(dir.join("cmdline"), b"python3\0card-watch.py\0--token\0x\0").unwrap();
        }
        let peer: SocketAddr = "127.0.0.1:40000".parse().unwrap();
        let found = process_of(root, peer, 7690).unwrap();
        assert_eq!(
            found,
            Process {
                pid: 20,
                ppid: 5,
                cwd: "/work/here".into(),
                program: "python3 card-watch.py".into(),
            }
        );
        // Another port of the Host's, or a caller nobody holds, is not found.
        assert_eq!(process_of(root, peer, 7691), None);
        assert_eq!(
            process_of(root, "127.0.0.1:40002".parse().unwrap(), 7690),
            None
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn an_ipv6_table_names_a_loopback_caller_too() {
        let peer: SocketAddr = "[::1]:40000".parse().unwrap();
        // ::1 as the kernel writes it on a little-endian machine.
        let ip = if cfg!(target_endian = "little") {
            "00000000000000000000000001000000"
        } else {
            "00000000000000000000000000000001"
        };
        let line = format!("h\n 0: {ip}:9C40 {ip}:1E0A 01 0:0 0:0 0 1000 0 55 1\n");
        assert_eq!(socket_inode(&line, peer, 7690).as_deref(), Some("55"));
    }
}
