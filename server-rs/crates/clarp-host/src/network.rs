//! Whether this machine reaches the provider of an agent's tool: what a turn whose connection
//! failed waits for (`clarp_kernel::recovery`). A laptop that changes networks has none for
//! half a minute or more, and its turns wait for it instead of failing.
//!
//! The check is this Host's own: the name of the provider's API is looked up, and a
//! connection is opened to it on port 443 and closed again, each within a few seconds. Nothing
//! is sent. Where the provider does not answer, two names that answer wherever there is a
//! network are tried: if one does, the network is there and the provider is the trouble, which
//! a minute of tries settles (`clarp_kernel::recovery`). A tool whose provider is not known
//! here is checked against those two alone. Where the Host's environment names a proxy
//! (`HTTPS_PROXY`, `https_proxy`, `ALL_PROXY`), the tools reach their providers through it: a
//! connection to the proxy is what is checked.

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

/// The variables a proxy for the tools' connections is named in, in the order they count.
const PROXY_VARIABLES: [&str; 3] = ["HTTPS_PROXY", "https_proxy", "ALL_PROXY"];

/// The check of a Host: of the real network, or of a switch a test holds.
pub fn checker(switch: Option<PathBuf>) -> Arc<dyn Check> {
    match switch {
        Some(path) => Arc::new(Switch(path)),
        None => {
            let named = PROXY_VARIABLES
                .iter()
                .find_map(|name| std::env::var(name).ok().filter(|value| !value.is_empty()));
            Arc::new(Probe {
                proxy: named.as_deref().and_then(proxy_address),
                reach: reaches,
            })
        }
    }
}

/// The network as this machine has it. `reach` says whether a connection to a name and port
/// opens.
struct Probe<R> {
    /// The proxy the tools connect through, where the environment names one.
    proxy: Option<(String, u16)>,
    reach: R,
}

impl<R: Fn(&str, u16) -> bool + Send + Sync> Check for Probe<R> {
    fn reachable(&self, backend: &str) -> bool {
        if let Some((host, port)) = &self.proxy {
            return (self.reach)(host, *port);
        }
        let providers = provider_hosts(backend);
        providers.iter().any(|host| (self.reach)(host, 443))
            || (providers != ANYWHERE && ANYWHERE.iter().any(|host| (self.reach)(host, 443)))
    }
}

/// The name and port of a proxy, from what a variable says of it: an address with or without
/// its scheme, the port by the scheme where it gives none.
fn proxy_address(named: &str) -> Option<(String, u16)> {
    let named = named.trim();
    let (scheme, rest) = named.split_once("://").unwrap_or(("http", named));
    let authority = rest.split('/').next()?;
    let authority = authority.rsplit('@').next()?;
    let default_port = match scheme.to_ascii_lowercase().as_str() {
        "https" => 443,
        scheme if scheme.starts_with("socks") => 1080,
        _ => 80,
    };
    let (host, port) = match authority.strip_prefix('[') {
        // An address of IPv6, in brackets.
        Some(bracketed) => {
            let (host, after) = bracketed.split_once(']')?;
            let port = after.strip_prefix(':').map(str::parse).transpose().ok()?;
            (host, port)
        }
        None => match authority.rsplit_once(':') {
            Some((host, port)) => (host, Some(port.parse().ok()?)),
            None => (authority, None),
        },
    };
    (!host.is_empty()).then(|| (host.to_string(), port.unwrap_or(default_port)))
}

/// Whether the name resolves, and a connection to one of its addresses opens on the port.
fn reaches(host: &str, port: u16) -> bool {
    resolve(host, port).iter().any(|address| {
        TcpStream::connect_timeout(address, CONNECT_WITHIN)
            .map(drop)
            .is_ok()
    })
}

/// The addresses of a name, looked up on a thread of its own: the system's resolver has no
/// time limit of its own, and one without a network can take long to give up. A look-up
/// that takes too long is left to finish by itself, and counts as none.
fn resolve(host: &str, port: u16) -> Vec<SocketAddr> {
    let (tx, rx) = mpsc::channel();
    let name = format!("{host}:{port}");
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
#[allow(clippy::unwrap_used)]
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

    /// What a probe of a test tried: names and ports, in order.
    type Tried = Arc<std::sync::Mutex<Vec<(String, u16)>>>;

    /// A probe whose connections open only to the names given, and that keeps what it tried.
    fn probe(
        proxy: Option<(&str, u16)>,
        open: &'static [(&'static str, u16)],
    ) -> (Probe<impl Fn(&str, u16) -> bool + Send + Sync>, Tried) {
        let tried = Tried::default();
        let kept = tried.clone();
        let probe = Probe {
            proxy: proxy.map(|(host, port)| (host.to_string(), port)),
            reach: move |host: &str, port: u16| {
                kept.lock().unwrap().push((host.to_string(), port));
                open.contains(&(host, port))
            },
        };
        (probe, tried)
    }

    #[test]
    fn the_network_is_there_where_the_provider_or_any_name_answers() {
        // The provider answers.
        let (check, tried) = probe(None, &[("api.anthropic.com", 443)]);
        assert!(check.reachable("claude"));
        assert_eq!(tried.lock().unwrap().len(), 1);
        // The provider does not, a name that answers anywhere does: the network is there, and
        // the provider is the trouble.
        let (check, _) = probe(None, &[("dns.google", 443)]);
        assert!(check.reachable("claude"));
        assert!(check.reachable("codex"));
        assert!(check.reachable("opencode"));
        // Nothing answers: it is gone, after every name was tried once.
        let (check, tried) = probe(None, &[]);
        assert!(!check.reachable("claude"));
        assert_eq!(
            *tried.lock().unwrap(),
            vec![
                ("api.anthropic.com".to_string(), 443),
                ("one.one.one.one".to_string(), 443),
                ("dns.google".to_string(), 443),
            ]
        );
        let (check, tried) = probe(None, &[]);
        assert!(!check.reachable("opencode"));
        assert_eq!(tried.lock().unwrap().len(), ANYWHERE.len());
    }

    #[test]
    fn behind_a_proxy_the_proxy_is_what_is_checked() {
        let (check, tried) = probe(Some(("proxy.corp", 3128)), &[("proxy.corp", 3128)]);
        assert!(check.reachable("claude"));
        assert_eq!(
            *tried.lock().unwrap(),
            vec![("proxy.corp".to_string(), 3128)],
            "the provider is not asked directly"
        );
        let (check, _) = probe(Some(("proxy.corp", 3128)), &[("api.anthropic.com", 443)]);
        assert!(!check.reachable("claude"), "a proxy that does not answer");
        // What the variables say of a proxy.
        let address = |named| proxy_address(named);
        assert_eq!(
            address("http://proxy.corp:3128"),
            Some(("proxy.corp".into(), 3128))
        );
        assert_eq!(
            address("http://user:secret@proxy.corp:3128/"),
            Some(("proxy.corp".into(), 3128))
        );
        assert_eq!(
            address("proxy.corp:8080"),
            Some(("proxy.corp".into(), 8080))
        );
        assert_eq!(
            address("https://proxy.corp"),
            Some(("proxy.corp".into(), 443))
        );
        assert_eq!(
            address("socks5://127.0.0.1"),
            Some(("127.0.0.1".into(), 1080))
        );
        assert_eq!(address("http://[::1]:3128"), Some(("::1".into(), 3128)));
        assert_eq!(address("http://proxy.corp:port"), None);
        assert_eq!(address(""), None);
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
