//! Whether this machine reaches the provider of an agent's tool: what a turn whose connection
//! failed waits for (`clarp_kernel::recovery`). A laptop that changes networks has none for
//! half a minute or more, and its turns wait for it instead of failing.
//!
//! The check is this Host's own: the name of the provider's API is looked up, and a
//! connection is opened to it on port 443 and closed again, each within a few seconds. Nothing
//! is sent. A tool whose provider is not known here is checked against two names that are
//! always there where there is a network at all.

use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::sync::{Arc, mpsc};
use std::time::Duration;

/// How long the look-up of a name may take before the check counts the network as gone.
const RESOLVE_WITHIN: Duration = Duration::from_secs(4);

/// How long a connection may take to open before the check counts the network as gone.
const CONNECT_WITHIN: Duration = Duration::from_secs(4);

/// Where a tool's provider is not known: names that answer wherever there is a network.
const ANYWHERE: &[&str] = &["one.one.one.one", "dns.google"];

/// Whether this machine reaches the provider of a tool.
pub trait Check: Send + Sync {
    /// Whether this machine reaches the provider of `backend`'s tool now. It can take some
    /// seconds: it is never asked on an agent's own thread.
    fn reachable(&self, backend: &str) -> bool;
}

/// The names of the API a tool's provider answers at: where the tool connects.
pub fn provider_hosts(backend: &str) -> &'static [&'static str] {
    match backend {
        "claude" => &["api.anthropic.com"],
        // Codex signed in with ChatGPT talks to chatgpt.com; with a key, to the API.
        "codex" => &["chatgpt.com", "api.openai.com"],
        "grok" | "muse" => &["api.x.ai"],
        "agy" => &[
            "cloudcode-pa.googleapis.com",
            "generativelanguage.googleapis.com",
        ],
        _ => ANYWHERE,
    }
}

/// The check of a Host: of the real network, or of a switch a test holds.
pub fn checker(switch: Option<PathBuf>) -> Arc<dyn Check> {
    match switch {
        Some(path) => Arc::new(Switch(path)),
        None => Arc::new(Probe),
    }
}

/// The network as this machine has it.
struct Probe;

impl Check for Probe {
    fn reachable(&self, backend: &str) -> bool {
        provider_hosts(backend).iter().any(|host| reaches(host))
    }
}

/// Whether the name resolves, and a connection to one of its addresses opens on 443.
fn reaches(host: &str) -> bool {
    resolve(host).iter().any(|address| {
        TcpStream::connect_timeout(address, CONNECT_WITHIN)
            .map(drop)
            .is_ok()
    })
}

/// The addresses of a name, looked up on a thread of its own: the system's resolver has no
/// time limit of its own, and one without a network can take long to give up. A look-up
/// that takes too long is left to finish by itself, and counts as none.
fn resolve(host: &str) -> Vec<SocketAddr> {
    let (tx, rx) = mpsc::channel();
    let name = format!("{host}:443");
    let spawned = std::thread::Builder::new()
        .name("network-resolve".into())
        .spawn(move || {
            let found = name
                .to_socket_addrs()
                .map(|addresses| addresses.collect::<Vec<_>>());
            let _ = tx.send(found.unwrap_or_default());
        });
    if spawned.is_err() {
        return Vec::new();
    }
    rx.recv_timeout(RESOLVE_WITHIN).unwrap_or_default()
}

/// A network a test holds: gone while the file is there, and there while it is not.
struct Switch(PathBuf);

impl Check for Switch {
    fn reachable(&self, _backend: &str) -> bool {
        !self.0.exists()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_tool_is_checked_against_its_providers_api_and_an_unknown_one_against_any() {
        assert_eq!(provider_hosts("claude"), &["api.anthropic.com"]);
        assert!(provider_hosts("codex").contains(&"chatgpt.com"));
        assert_eq!(provider_hosts("grok"), &["api.x.ai"]);
        assert_eq!(provider_hosts("opencode"), ANYWHERE);
        assert_eq!(provider_hosts("a tool nobody has heard of"), ANYWHERE);
    }

    #[test]
    fn a_tests_switch_holds_the_network() {
        let place = std::env::temp_dir().join(format!("clarp-network-{}", std::process::id()));
        let check = checker(Some(place.clone()));
        assert!(check.reachable("claude"));
        std::fs::write(&place, "").unwrap();
        assert!(!check.reachable("claude"));
        std::fs::remove_file(&place).unwrap();
        assert!(check.reachable("claude"));
    }
}
