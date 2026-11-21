//! Where an HTTP URL sends its request, parsed once (R133, the Python Host's PR 468:
//! `server/lib/url_destination.py`).
//!
//! An allow-list compares destinations, not text: a URL can begin with an allowed prefix as text
//! while its parsed host is another. [`destination`] parses a URL once into the parts a request
//! goes to (as Python's `urllib.parse.urlsplit` splits it, whose checks are kept here); [`allowed`]
//! compares those parts with allowed prefixes parsed the same way and answers the URL rebuilt from
//! them, which is the URL the caller sends. Held by `tests/fixtures/controller-actions-vectors.json`.

use std::net::Ipv6Addr;

/// The schemes a destination may have, with their own ports.
const SCHEMES: [(&str, u16); 2] = [("http", 80), ("https", 443)];

/// The loopback prefixes every Host allows, of any port of their host
/// (`controller_actions.DEFAULT_ALLOWED_URLS`).
pub const LOOPBACK: [&str; 3] = ["http://127.0.0.1", "http://localhost", "http://[::1]"];

/// A parsed destination.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Destination {
    pub scheme: String,
    /// Case-folded; an IPv6 literal without its brackets.
    pub host: String,
    /// Made explicit: the scheme's own where the URL names none.
    pub port: u16,
    /// `/` where the URL has none.
    pub path: String,
    pub query: String,
}

fn own_port(scheme: &str) -> u16 {
    SCHEMES
        .iter()
        .find(|(known, _)| *known == scheme)
        .map_or(0, |(_, port)| *port)
}

impl Destination {
    /// The URL rebuilt from the parsed parts (no user information, no fragment); the port is
    /// named only when it is not the scheme's own, so that the request's Host is the plain one.
    pub fn url(&self) -> String {
        let host = if self.host.contains(':') {
            format!("[{}]", self.host)
        } else {
            self.host.clone()
        };
        let netloc = if self.port == own_port(&self.scheme) {
            host
        } else {
            format!("{host}:{}", self.port)
        };
        let mut url = format!("{}://{netloc}{}", self.scheme, self.path);
        if !self.query.is_empty() {
            url.push('?');
            url.push_str(&self.query);
        }
        url
    }

    /// Same scheme, host and port (any port with `any_port`), and the path on a path boundary
    /// of the prefix's.
    pub fn under(&self, prefix: &Destination, any_port: bool) -> bool {
        if (&self.scheme, &self.host) != (&prefix.scheme, &prefix.host) {
            return false;
        }
        if !any_port && self.port != prefix.port {
            return false;
        }
        let base = prefix.path.trim_end_matches('/');
        base.is_empty()
            || self.path == base
            || self
                .path
                .strip_prefix(base)
                .is_some_and(|tail| tail.starts_with('/'))
    }
}

/// Python's `str.isspace`, for what a URL may not hold.
fn python_space(sign: char) -> bool {
    sign.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&sign)
}

/// Python's `urlsplit` of a URL that holds no space and no control sign: its scheme, its
/// network location, path and query; `None` where it raises.
fn split(url: &str) -> Option<(String, String, String, String)> {
    let mut scheme = String::new();
    let mut rest = url;
    if let Some(at) = url.find(':')
        && at > 0
        && url.starts_with(|sign: char| sign.is_ascii_alphabetic())
        && url[..at]
            .chars()
            .all(|sign| sign.is_ascii_alphanumeric() || "+-.".contains(sign))
    {
        scheme = url[..at].to_ascii_lowercase();
        rest = &url[at + 1..];
    }
    let mut netloc = "";
    if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        netloc = &after[..end];
        rest = &after[end..];
        let (open, close) = (netloc.contains('['), netloc.contains(']'));
        if open != close {
            return None;
        }
        if open {
            bracketed_netloc(netloc)?;
        }
    }
    let rest = rest.split_once('#').map_or(rest, |(before, _)| before);
    let (path, query) = rest.split_once('?').unwrap_or((rest, ""));
    Some((
        scheme,
        netloc.to_string(),
        path.to_string(),
        query.to_string(),
    ))
}

/// `_check_bracketed_netloc`: nothing before the bracket, nothing but a port after it, and in
/// it an address of IPv6 (or of a version to come), never one of IPv4.
fn bracketed_netloc(netloc: &str) -> Option<()> {
    let host_and_port = netloc.rsplit_once('@').map_or(netloc, |(_, after)| after);
    let hostname = match host_and_port.split_once('[') {
        Some((before, bracketed)) => {
            if !before.is_empty() {
                return None;
            }
            let (hostname, port) = bracketed.split_once(']').unwrap_or((bracketed, ""));
            if !port.is_empty() && !port.starts_with(':') {
                return None;
            }
            hostname
        }
        None => host_and_port
            .split_once(':')
            .map_or(host_and_port, |(host, _)| host),
    };
    if hostname.starts_with(['v', 'V']) {
        // `\A[vV][a-fA-F0-9]+\..+\Z`
        let (version, after) = hostname[1..].split_once('.')?;
        let hex = !version.is_empty() && version.chars().all(|sign| sign.is_ascii_hexdigit());
        return (hex && !after.is_empty() && !after.contains('\n')).then_some(());
    }
    // An address of IPv4 is refused in brackets: it is no address of IPv6.
    ipv6(hostname).map(|_| ())
}

/// An IPv6 address as Python's `ipaddress.IPv6Address` reads and writes it, with its zone.
fn ipv6(text: &str) -> Option<String> {
    let (address, zone) = match text.split_once('%') {
        Some((address, zone)) => {
            if zone.is_empty() || zone.contains('%') {
                return None;
            }
            (address, Some(zone))
        }
        None => (text, None),
    };
    let parsed: Ipv6Addr = address.parse().ok()?;
    Some(match zone {
        Some(zone) => format!("{parsed}%{zone}"),
        None => parsed.to_string(),
    })
}

/// What a host may be: plain labels of letters, digits and inner hyphens, or an IPv6 literal.
/// Another spelling of an address is a name like any other.
fn host(raw: &str) -> Option<String> {
    if raw.contains(':') {
        return ipv6(raw);
    }
    // An empty label (a trailing dot, two dots in a row) is no label, as the reference's
    // pattern has it: a host that ends in a dot is refused by that alone.
    let label = |label: &str| {
        let bytes = label.as_bytes();
        let inner = |byte: &u8| byte.is_ascii_lowercase() || byte.is_ascii_digit();
        !bytes.is_empty()
            && bytes.len() <= 63
            && inner(&bytes[0])
            && inner(&bytes[bytes.len() - 1])
            && bytes.iter().all(|byte| inner(byte) || *byte == b'-')
    };
    raw.split('.').all(label).then(|| raw.to_string())
}

/// Whether a path, read as a server might read it, has a segment `.` or `..` (R139, the
/// Python Host's `_dot_segment`): the encoded dot, slash and backslash decoded once, in either
/// case (never twice, so an encoded `%` stays what it is), a backslash read as a slash, and each
/// segment cut at its first `;` (a server that drops a segment's parameters before resolving
/// reads `..;x` as `..`). Only the test reads it so: what is sent is the path as given.
pub fn dot_segment(path: &str) -> bool {
    let mut read = String::with_capacity(path.len());
    let mut rest = path;
    while let Some(at) = rest.find('%') {
        read.push_str(&rest[..at]);
        let after = &rest[at + 1..];
        let decoded = after
            .get(..2)
            .and_then(|hex| match hex.to_ascii_lowercase().as_str() {
                "2e" => Some('.'),
                "2f" => Some('/'),
                "5c" => Some('\\'),
                _ => None,
            });
        match decoded {
            Some(sign) => {
                read.push(sign);
                rest = &after[2..];
            }
            None => {
                read.push('%');
                rest = after;
            }
        }
    }
    read.push_str(rest);
    read.replace('\\', "/").split('/').any(|segment| {
        let segment = segment.split(';').next().unwrap_or_default();
        segment == "." || segment == ".."
    })
}

/// The parsed destination of an http(s) URL, or `None`: not http(s), no host, user information
/// in the authority, a port that is not a number, a host that is not a plain name or IP
/// address, or a URL that does not parse.
pub fn destination(url: &str) -> Option<Destination> {
    if url
        .chars()
        .any(|sign| python_space(sign) || u32::from(sign) < 32)
        || url.contains('\\')
    {
        return None;
    }
    let (scheme, netloc, path, query) = split(url)?;
    // R139: a path with a dot segment, read as a server might read it.
    if dot_segment(&path) {
        return None;
    }
    // `_hostinfo`: after the user information, a bracketed host or one up to its colon.
    let hostinfo = netloc
        .rsplit_once('@')
        .map_or(netloc.as_str(), |(_, after)| after);
    let (hostname, port) = match hostinfo.split_once('[') {
        Some((_, bracketed)) => {
            let (hostname, after) = bracketed.split_once(']').unwrap_or((bracketed, ""));
            (hostname, after.split_once(':').map_or("", |(_, port)| port))
        }
        None => hostinfo.split_once(':').unwrap_or((hostinfo, "")),
    };
    let port = if port.is_empty() {
        None
    } else {
        if !port.chars().all(|sign| sign.is_ascii_digit()) {
            return None;
        }
        Some(port.parse::<u16>().ok()?)
    };
    let own = SCHEMES.iter().find(|(known, _)| *known == scheme)?.1;
    if netloc.contains('@') || hostname.is_empty() {
        return None;
    }
    // Brackets around a name (the grammar's address of a version to come): only an address of
    // IPv6 has its brackets.
    if (netloc.contains('[') || netloc.contains(']')) && !hostname.contains(':') {
        return None;
    }
    let host = host(&hostname.to_lowercase())?;
    Some(Destination {
        scheme,
        host,
        port: port.unwrap_or(own),
        path: if path.is_empty() {
            "/".to_string()
        } else {
            path
        },
        query,
    })
}

/// An allowed prefix, parsed as a URL: refused (`None`) as a URL is, and when it carries a
/// query or a fragment, which a prefix cannot mean.
pub fn prefix(text: &str) -> Option<Destination> {
    let found = destination(text.trim_matches(python_space))?;
    (found.query.is_empty() && !text.contains('#')).then_some(found)
}

/// The URL to send when `url`'s parsed destination is under one of `prefixes`, or of the
/// [`LOOPBACK`] prefixes (of any port of their host); else `None`.
pub fn allowed(url: &str, prefixes: &[Destination]) -> Option<String> {
    let found = destination(url)?;
    let loopback = LOOPBACK.iter().filter_map(|text| prefix(text));
    let any_port = loopback.map(|prefix| (prefix, true));
    let own_port = prefixes.iter().cloned().map(|prefix| (prefix, false));
    any_port
        .chain(own_port)
        .any(|(prefix, every_port)| found.under(&prefix, every_port))
        .then(|| found.url())
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn a_path_with_a_dot_segment_is_refused_in_every_spelling_before_anything_is_resolved() {
        for path in [
            "/api/./x",
            "/api/../x",
            "/api/%2e%2e/x",
            "/api/%2E%2e/x",
            "/api/.%2E/x",
            "/api/%2e/x",
            "/api/..",
            "/api/.",
            "/..",
            "/.",
            "/api/sub/../one",
            "/api/%2e./x",
            "/api/x/./",
            "/api/..;x/y",
            "/api/.;x/y",
            "/api/%2e%2e;x/y",
            "/api/a%2F..%2Fb",
            "/api/a%2f..%2fb",
            "/api/a%5C..%5Cb",
            "/api/a%5c%2e%2e%5cb",
        ] {
            assert!(dot_segment(path), "{path}");
            let url = format!("https://service.invalid{path}");
            assert_eq!(destination(&url), None, "{url}");
            assert_eq!(prefix(&url), None, "{url}");
            let under = [prefix("https://service.invalid/api").unwrap()];
            assert_eq!(allowed(&url, &under), None, "{url}");
        }
        // Dots that make no segment of their own, an encoded slash with no dot segment beside
        // it, what is encoded twice (decoded once, it is no dot), a parameter that follows no
        // dot: a path like any other, sent byte for byte.
        for path in [
            "/api/..x",
            "/api/x..",
            "/api/...",
            "/api/%2e%2ex",
            "/api/x.y",
            "/api/.well-known",
            "/api/%2e%2e%2e",
            "/api/a%2Fb",
            "/api/%252e%252e/x",
            "/api/a%252F..%252Fb",
            "/api/report;v=2",
        ] {
            assert!(!dot_segment(path), "{path}");
            let url = format!("https://service.invalid{path}");
            assert!(destination(&url).is_some(), "{url}");
            let under = [prefix("https://service.invalid/api").unwrap()];
            assert_eq!(allowed(&url, &under), Some(url.clone()), "{url}");
        }
        // A raw backslash anywhere, before anything is split; brackets around a name.
        for url in [
            "https://service.invalid/api/a\\b",
            "https://service.invalid/api/a\\..\\b",
            "https://service.invalid/api/x?a=\\",
            "http://[v1.service.invalid]/x",
        ] {
            assert_eq!(destination(url), None, "{url}");
            assert_eq!(prefix(url), None, "{url}");
        }
        assert!(
            destination("http://[::1]:9/x").is_some(),
            "an IPv6 address is a host"
        );
        assert!(destination("https://service.invalid/api?x=/../y").is_some());
        assert_eq!(
            allowed("http://127.0.0.1:8123/a/../b", &[]),
            None,
            "the loopback prefixes too"
        );
    }
}
