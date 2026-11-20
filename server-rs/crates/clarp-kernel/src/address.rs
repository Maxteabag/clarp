//! An address as a client will reach it: parsed once, and rebuilt from its parsed parts, so
//! that what is judged is what is dialled. A text is never judged by how it begins: a text
//! that begins as an address on this machine can name another host to a client that parses
//! it (the class of defect R133).

/// Parse the input once. Build the outbound client's URI from the canonical URL
/// components, including its normalized path. Only this URI can leave admission.
#[derive(Clone, Debug)]
pub struct Address(http::Uri);

impl Address {
    /// An address of the web (`http`, `https`).
    pub fn parse(raw: &str) -> Option<Self> {
        Self::parse_in(raw, &["http", "https"])
    }

    /// An address of one of `schemes`. Refused: user information, a backslash, a control
    /// character, an empty authority or one that ends at its colon.
    pub fn parse_in(raw: &str, schemes: &[&str]) -> Option<Self> {
        let (scheme, rest) = raw.split_once("://")?;
        if !schemes
            .iter()
            .any(|known| scheme.eq_ignore_ascii_case(known))
        {
            return None;
        }
        let authority = rest.split(['/', '?', '#']).next()?;
        if authority.is_empty()
            || authority.contains('@')
            || authority.ends_with(':')
            || raw.contains('\\')
            || raw.chars().any(char::is_control)
        {
            return None;
        }
        let parsed = url::Url::parse(raw).ok()?;
        if !schemes.contains(&parsed.scheme())
            || !parsed.username().is_empty()
            || parsed.password().is_some()
        {
            return None;
        }
        // Url canonicalizes a default port away; an explicitly configured port
        // still restricts the allow-list entry to that port.
        let port = authority
            .rsplit_once(':')
            .and_then(|(_, s)| s.parse::<u16>().ok());
        let mut host = parsed.host()?.to_string();
        if let Some(port) = port {
            host.push_str(&format!(":{port}"));
        }
        let uri = http::Uri::builder()
            .scheme(parsed.scheme())
            .authority(host)
            .path_and_query(&parsed[url::Position::BeforePath..url::Position::AfterQuery])
            .build()
            .ok()?;
        Some(Self(uri))
    }

    fn admits(&self, addr: &Self) -> bool {
        let (prefix, target) = (&self.0, &addr.0);
        let default = if target.scheme_str() == Some("https") {
            443
        } else {
            80
        };
        if prefix.scheme() != target.scheme()
            || prefix.host() != target.host()
            || prefix
                .port_u16()
                .is_some_and(|p| p != target.port_u16().unwrap_or(default))
        {
            return false;
        }
        let want = prefix.path_and_query().map_or("/", |p| p.as_str());
        let path = target.path_and_query().map_or("/", |p| p.as_str());
        path.strip_prefix(want).is_some_and(|tail| {
            tail.is_empty() || want.ends_with('/') || tail.starts_with(['/', '?', '#'])
        })
    }

    /// Whether the parsed host is this machine's own name for itself.
    fn loopback(&self) -> bool {
        matches!(self.0.host(), Some("127.0.0.1" | "localhost" | "[::1]"))
    }

    pub fn allowed(raw: &str, entries: &[Self]) -> Option<http::Uri> {
        let address = Self::parse(raw)?;
        let loopback = address.0.scheme_str() == Some("http") && address.loopback();
        (loopback || entries.iter().any(|entry| entry.admits(&address))).then_some(address.0)
    }
}

/// What an address on this machine may be, for one variable: its scheme (`http` for a
/// stand-in over the web, `ws` for one reached by a socket), whether it names its port (a
/// non-zero one), and whether it may carry a path and a query.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Local {
    pub scheme: &'static str,
    pub port: bool,
    pub path: bool,
    pub query: bool,
}

impl Local {
    /// A stand-in over the web, a port named, a path and a query as the caller joins them.
    pub const WEB: Self = Self {
        scheme: "http",
        port: true,
        path: true,
        query: true,
    };
    /// A stand-in reached by a socket, a port named.
    pub const SOCKET: Self = Self {
        scheme: "ws",
        ..Self::WEB
    };
}

/// An address on this machine (`rule`): parsed once ([`Address`]: no user information, no
/// backslash, no control character), its host this machine's own name for itself, and
/// rebuilt from its parsed parts. The text a variable keeps is this one, never the text as it
/// was given: only the address that was judged can reach a client. Where the given text named
/// no path, none is added.
pub fn on_this_machine(raw: &str, rule: Local) -> Option<String> {
    let address = Address::parse_in(raw, &[rule.scheme])?;
    let uri = &address.0;
    if !address.loopback() {
        return None;
    }
    let port = uri.port_u16();
    if rule.port && port.is_none_or(|port| port == 0) {
        return None;
    }
    let (_, rest) = raw.split_once("://")?;
    let named_path = rest.find(['/', '?', '#']).is_some();
    if !rule.path && named_path {
        return None;
    }
    if !rule.query && uri.query().is_some() {
        return None;
    }
    let host = uri.host()?;
    let mut built = format!("{}://{host}", rule.scheme);
    if let Some(port) = port {
        built.push_str(&format!(":{port}"));
    }
    if named_path {
        built.push_str(uri.path_and_query().map_or("/", |p| p.as_str()));
    }
    Some(built)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn an_address_on_this_machine_is_judged_parsed_and_kept_as_it_was_rebuilt() {
        let web = Local::WEB;
        // On this machine, by each of its names; kept rebuilt.
        for (given, kept) in [
            ("http://127.0.0.1:5000", "http://127.0.0.1:5000"),
            ("http://localhost:5000/judge", "http://localhost:5000/judge"),
            ("http://[::1]:5000/a?b=1", "http://[::1]:5000/a?b=1"),
            ("HTTP://127.0.0.1:5000/x", "http://127.0.0.1:5000/x"),
            ("http://127.0.0.1:5000/a/../b", "http://127.0.0.1:5000/b"),
        ] {
            assert_eq!(
                on_this_machine(given, web).as_deref(),
                Some(kept),
                "{given}"
            );
        }
        // Refused: user information (before a host that is this machine, refused all the
        // same), a backslash, a control character, another host, another scheme, no port,
        // port zero, an authority that ends at its colon.
        for refused in [
            "http://someone@127.0.0.1:5000",
            "http://someone:secret@127.0.0.1:5000/x",
            "http://@127.0.0.1:5000",
            "http://127.0.0.1:5000\\x",
            "http://127.0.0.1:5000/a\\b",
            "http://127.0.0.1:5000/x\ty",
            "http://example.invalid:5000",
            "http://10.0.0.1:5000",
            "https://127.0.0.1:5000",
            "ws://127.0.0.1:5000",
            "http://127.0.0.1",
            "http://127.0.0.1:0",
            "http://127.0.0.1:",
            "127.0.0.1:5000",
            "",
        ] {
            assert_eq!(on_this_machine(refused, web), None, "{refused:?}");
        }
        // A socket keeps its own scheme under the same rule.
        assert_eq!(
            on_this_machine("ws://127.0.0.1:7/live", Local::SOCKET).as_deref(),
            Some("ws://127.0.0.1:7/live")
        );
        assert_eq!(
            on_this_machine("http://127.0.0.1:7/live", Local::SOCKET),
            None
        );
        assert_eq!(
            on_this_machine("ws://someone@127.0.0.1:7", Local::SOCKET),
            None
        );
        // A rule that takes no path or no query.
        let bare = Local {
            path: false,
            query: false,
            ..Local::WEB
        };
        assert_eq!(
            on_this_machine("http://127.0.0.1:9", bare).as_deref(),
            Some("http://127.0.0.1:9")
        );
        assert_eq!(on_this_machine("http://127.0.0.1:9/", bare), None);
        assert_eq!(on_this_machine("http://127.0.0.1:9/x", bare), None);
        assert_eq!(on_this_machine("http://127.0.0.1:9/?q=1", bare), None);
        assert_eq!(on_this_machine("http://127.0.0.1:9?q=1", bare), None);
        assert_eq!(on_this_machine("http://127.0.0.1:9#x", bare), None);
        let no_query = Local {
            query: false,
            ..Local::SOCKET
        };
        assert_eq!(
            on_this_machine("ws://127.0.0.1:9/live", no_query).as_deref(),
            Some("ws://127.0.0.1:9/live")
        );
        assert_eq!(
            on_this_machine("ws://127.0.0.1:9/live?key=1", no_query),
            None
        );
    }
}
