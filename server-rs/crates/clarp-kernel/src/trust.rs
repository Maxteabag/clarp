//! Who a request is trusted as, by where it came from.
//!
//! A socket from this machine is not proof of a person at this machine: a proxy on it, and the
//! relay's connector, come from this machine too. So what a request says of how it travelled
//! can only take trust away. A request is local when it comes from this machine and carries
//! no sign of a proxy, of the relay, or of a page loaded from somewhere else. Only a local
//! request may present the administrator's token, a token in its address, or the token of an
//! agent's tools.
//!
//! These are the Python Host's rules (`server/lib/request_security.py`), held to it by
//! `tests/connector_vectors.rs`: which peers are this machine, which requests are local, whose
//! failure a failed authentication is, and when failures are answered with a wait. The
//! addresses in a text are read as Python's `ipaddress` and `urllib.parse.urlsplit` read them,
//! because what those take for this machine is what the rule is.

use std::collections::VecDeque;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

/// An address read as Python's `ipaddress.ip_address` reads a text: an IPv4 address, or an
/// IPv6 address that may name its zone after a `%`.
fn read_address(text: &str) -> Option<(IpAddr, Option<&str>)> {
    if let Ok(address) = text.parse::<Ipv4Addr>() {
        return Some((IpAddr::V4(address), None));
    }
    let (address, zone) = match text.split_once('%') {
        // A zone is named in full, once.
        Some((_, zone)) if zone.is_empty() || zone.contains('%') => return None,
        Some((address, zone)) => (address, Some(zone)),
        None => (text, None),
    };
    let address = address.parse::<Ipv6Addr>().ok()?;
    Some((IpAddr::V6(address), zone))
}

/// An address as Python writes it (`str(ipaddress.ip_address(text))`): `None` for what is no
/// address. One address, with nothing around it.
pub fn ip_address(text: &str) -> Option<String> {
    let (address, zone) = read_address(text)?;
    Some(match zone {
        Some(zone) => format!("{address}%{zone}"),
        None => address.to_string(),
    })
}

/// Whether a peer's address is this machine itself. What follows a `%` names a zone and is
/// not looked at; an IPv4 address written as an IPv6 one counts as the IPv4 address it is.
pub fn is_loopback(peer: &str) -> bool {
    let address = peer.split('%').next().unwrap_or_default();
    match read_address(address) {
        Some((IpAddr::V4(address), _)) => address.is_loopback(),
        Some((IpAddr::V6(address), _)) => match address.to_ipv4_mapped() {
            Some(mapped) => mapped.is_loopback(),
            None => address.is_loopback(),
        },
        None => false,
    }
}

/// An address split into its parts, as Python's `urllib.parse.urlsplit` splits it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Url {
    pub scheme: String,
    pub netloc: String,
    pub path: String,
    pub query: String,
    pub fragment: String,
}

/// The letters a scheme is written in.
fn in_a_scheme(sign: char) -> bool {
    sign.is_ascii_alphanumeric() || matches!(sign, '+' | '-' | '.')
}

/// What stands in brackets where a host is named: an IPv6 address, or an address of a kind
/// that is to come. `Err`: Python's words for what it is not.
fn bracketed(host: &str) -> Result<(), String> {
    if host.starts_with(['v', 'V']) {
        // `[vV][a-fA-F0-9]+\..+`
        let after = &host[1..];
        let digits = after.chars().take_while(char::is_ascii_hexdigit).count();
        let rest = &after[digits..];
        if digits == 0 || !rest.starts_with('.') || rest.len() < 2 {
            return Err("IPvFuture address is invalid".to_string());
        }
        return Ok(());
    }
    match read_address(host) {
        Some((IpAddr::V6(_), _)) => Ok(()),
        Some((IpAddr::V4(_), _)) => Err("An IPv4 address cannot be in brackets".to_string()),
        None => Err(format!(
            "'{host}' does not appear to be an IPv4 or IPv6 address"
        )),
    }
}

/// Splits an address. `Err`: why Python refuses it, in its words.
///
/// One thing is read otherwise, and said here: where a host is named in letters outside
/// ASCII, Python looks whether they would turn into a sign that parts an address, and this
/// Host does not read such a name at all. Written in its ASCII form it is read.
pub fn urlsplit(url: &str) -> Result<Url, String> {
    // What stands before an address is not part of it, and neither is a line end inside it.
    let url = url.trim_start_matches(|sign: char| sign <= ' ');
    let url: String = url
        .chars()
        .filter(|sign| !matches!(sign, '\t' | '\r' | '\n'))
        .collect();
    let mut rest = url.as_str();
    let mut scheme = String::new();
    if let Some(colon) = rest.find(':')
        && colon > 0
        && rest.starts_with(|sign: char| sign.is_ascii_alphabetic())
        && rest[..colon].chars().all(in_a_scheme)
    {
        scheme = rest[..colon].to_ascii_lowercase();
        rest = &rest[colon + 1..];
    }
    let mut netloc = "";
    if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        (netloc, rest) = after.split_at(end);
        let (open, close) = (netloc.contains('['), netloc.contains(']'));
        if open != close {
            return Err("Invalid IPv6 URL".to_string());
        }
        if open {
            let host_and_port = netloc.rsplit_once('@').map_or(netloc, |(_, host)| host);
            let host = match host_and_port.split_once('[') {
                Some((before, inside)) => {
                    // Nothing stands before a bracket, and after it only a port.
                    let (host, port) = inside.split_once(']').unwrap_or((inside, ""));
                    if !before.is_empty() || !(port.is_empty() || port.starts_with(':')) {
                        return Err("Invalid IPv6 URL".to_string());
                    }
                    host
                }
                None => host_and_port.split(':').next().unwrap_or_default(),
            };
            bracketed(host)?;
        }
    }
    if !netloc.is_ascii() {
        return Err("a host is named in ASCII".to_string());
    }
    let (rest, fragment) = rest.split_once('#').unwrap_or((rest, ""));
    let (path, query) = rest.split_once('?').unwrap_or((rest, ""));
    Ok(Url {
        scheme,
        netloc: netloc.to_string(),
        path: path.to_string(),
        query: query.to_string(),
        fragment: fragment.to_string(),
    })
}

impl Url {
    /// The host and the port, as they are written after whoever is named before an `@`.
    fn host_and_port(&self) -> (&str, Option<&str>) {
        let named = &self.netloc;
        let hostinfo = named
            .rsplit_once('@')
            .map_or(named.as_str(), |(_, host)| host);
        let (host, port) = match hostinfo.split_once('[') {
            Some((_, inside)) => {
                let (host, after) = inside.split_once(']').unwrap_or((inside, ""));
                (host, after.split_once(':').map_or("", |(_, port)| port))
            }
            None => hostinfo.split_once(':').unwrap_or((hostinfo, "")),
        };
        (host, (!port.is_empty()).then_some(port))
    }

    /// The host an address names, in small letters; the name of a zone stays as it is.
    pub fn hostname(&self) -> Option<String> {
        let (host, _) = self.host_and_port();
        if host.is_empty() {
            return None;
        }
        Some(match host.split_once('%') {
            Some((host, zone)) => format!("{}%{zone}", host.to_ascii_lowercase()),
            None => host.to_ascii_lowercase(),
        })
    }

    /// Whether somebody is named before the host (`user@`, `user:password@`).
    pub fn names_somebody(&self) -> bool {
        self.netloc.contains('@')
    }

    /// The port an address names. `Err`: what is written there is no port.
    pub fn port(&self) -> Result<Option<u16>, NoPort> {
        let (_, Some(port)) = self.host_and_port() else {
            return Ok(None);
        };
        if !port.bytes().all(|sign| sign.is_ascii_digit()) {
            return Err(NoPort);
        }
        // Zeros before a number do not make it larger.
        let digits = port.trim_start_matches('0');
        if digits.len() > 5 {
            return Err(NoPort);
        }
        let number: u32 = if digits.is_empty() {
            0
        } else {
            digits.parse().map_err(|_| NoPort)?
        };
        u16::try_from(number).map(Some).map_err(|_| NoPort)
    }
}

/// What stands where an address names its port is no port.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NoPort;

/// The first header of a name, whatever the case of its letters.
fn first<'a>(headers: &'a [(String, String)], name: &str) -> Option<&'a str> {
    let mut named = headers.iter();
    let found = named.find(|(header, _)| header.eq_ignore_ascii_case(name));
    found.map(|(_, value)| value.as_str())
}

/// The beginnings of the names a proxy on this machine writes into a request, and the names
/// that say a request was handed on.
const HANDED_ON_BEGINS: [&str; 3] = ["x-forwarded-", "tailscale-", "cf-"];
const HANDED_ON: [&str; 3] = ["forwarded", "x-real-ip", "x-clarp-transport"];

/// Whether a request is local: from this machine, by a program on it or a page it served,
/// and handed on by nobody. `headers` are the request's own, as they were sent.
pub fn local_request(peer: &str, headers: &[(String, String)]) -> bool {
    if !is_loopback(peer) {
        return false;
    }
    // A page says where it was loaded from: only one loaded from this machine is local.
    if let Some(origin) = first(headers, "origin")
        && !origin.is_empty()
    {
        let Ok(from) = urlsplit(origin) else {
            return false;
        };
        let host = from.hostname().unwrap_or_default();
        let here = host == "localhost" || is_loopback(&host);
        if !matches!(from.scheme.as_str(), "http" | "https") || !here {
            return false;
        }
    }
    let site = first(headers, "sec-fetch-site").unwrap_or_default();
    if site.eq_ignore_ascii_case("cross-site") {
        return false;
    }
    !headers.iter().any(|(name, _)| {
        let name = name.to_ascii_lowercase();
        HANDED_ON.contains(&name.as_str())
            || HANDED_ON_BEGINS.iter().any(|begin| name.starts_with(begin))
    })
}

/// Whose failure a failed authentication is: the peer's, unless the peer is this machine and
/// says for whom it asks. Only a proxy on this machine is believed about that: a caller from
/// elsewhere cannot spread its failures over addresses it makes up.
pub fn failure_source(peer: &str, headers: &[(String, String)]) -> String {
    if is_loopback(peer) {
        let said = first(headers, "x-forwarded-for").unwrap_or_default();
        let last = said.rsplit(',').next().unwrap_or_default();
        if let Some(address) = ip_address(crate::python::strip(last)) {
            return address;
        }
    }
    peer.to_string()
}

/// How many failed authentications one asker has before it is told to wait, in how long.
pub const FAILURES_MOST: u32 = 20;
pub const FAILURES_WINDOW_S: f64 = 60.0;
/// How many askers are remembered: a scan from many addresses cannot fill the memory.
pub const FAILURE_SOURCES_MOST: usize = 4096;

/// A budget of failed authentications for each asker, in windows of a fixed length. Nobody
/// is locked out: a right token is taken whatever came before, and nothing sleeps.
#[derive(Debug, Clone)]
pub struct FailureLimiter {
    limit: u32,
    window: f64,
    max_sources: usize,
    /// Who failed, when its window began, and how often in it. Who failed longest ago is first.
    sources: VecDeque<(String, f64, u32)>,
}

impl Default for FailureLimiter {
    fn default() -> Self {
        Self::new(FAILURES_MOST, FAILURES_WINDOW_S, FAILURE_SOURCES_MOST)
    }
}

impl FailureLimiter {
    pub fn new(limit: u32, window: f64, max_sources: usize) -> Self {
        Self {
            limit,
            window,
            max_sources,
            sources: VecDeque::new(),
        }
    }

    /// One more failure of `source`, at `now` seconds of a clock that only goes forward.
    /// Answers how many seconds the asker is told to wait: nothing while it is within its
    /// budget.
    pub fn failure(&mut self, source: &str, now: f64) -> u64 {
        let known = self.sources.iter().position(|(who, _, _)| who == source);
        let (mut start, mut count) = match known.and_then(|at| self.sources.remove(at)) {
            Some((_, start, count)) => (start, count),
            None => (now, 0),
        };
        if now - start >= self.window {
            (start, count) = (now, 0);
        }
        count += 1;
        self.sources.push_back((source.to_string(), start, count));
        while self.sources.len() > self.max_sources {
            self.sources.pop_front();
        }
        if count <= self.limit {
            return 0;
        }
        let left = (self.window - (now - start)).ceil();
        if left >= 1.0 { left as u64 } else { 1 }
    }

    /// Who is remembered, who failed longest ago first.
    pub fn remembered(&self) -> Vec<&str> {
        let remembered = self.sources.iter();
        remembered.map(|(who, _, _)| who.as_str()).collect()
    }
}
