//! Exact allow-list for the web app's two cross-origin reads.

use std::collections::BTreeSet;
use std::net::Ipv6Addr;
use std::path::Path;

use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use clarp_kernel::python;

type Origins = BTreeSet<String>;

const REQUEST_METHOD: &str = "access-control-request-method";
const REQUEST_HEADERS: &str = "access-control-request-headers";

/// Normalize configuration, preserving the spelling of the host apart from lowercase.
/// A URL parser that resolves dot segments or rewrites a numeric host could allow an origin
/// other than the one the owner entered.
fn normalize(text: &str) -> Option<String> {
    let (scheme, authority) = python::strip(text).split_once("://")?;
    let scheme = scheme.to_ascii_lowercase();
    let default = match scheme.as_str() {
        "http" => 80,
        "https" => 443,
        _ => return None,
    };
    let authority = authority.strip_suffix('/').unwrap_or(authority);
    let (host, port) = if let Some(ip) = authority.strip_prefix('[') {
        let (address, tail) = ip.split_once(']')?;
        address.parse::<Ipv6Addr>().ok()?;
        let port = if tail.is_empty() {
            None
        } else {
            Some(tail.strip_prefix(':')?)
        };
        (format!("[{address}]").to_ascii_lowercase(), port)
    } else {
        let (host, port) = authority
            .split_once(':')
            .map_or((authority, None), |(host, port)| (host, Some(port)));
        if host.is_empty()
            || !host
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_'))
        {
            return None;
        }
        (host.to_ascii_lowercase(), port)
    };
    let port = port.filter(|s| !s.is_empty()).map_or(Some(default), |s| {
        s.bytes()
            .all(|b| b.is_ascii_digit())
            .then(|| s.parse::<u16>().ok())
            .flatten()
    })?;
    let origin = format!("{scheme}://{host}");
    Some(if port == default {
        origin
    } else {
        format!("{origin}:{port}")
    })
}

pub(crate) fn read_config(path: &Path) -> Origins {
    let Some(file) = std::fs::read_to_string(path)
        .ok()
        .and_then(|text| text.parse::<toml::Value>().ok())
    else {
        return Origins::new();
    };
    let Some(value) = file
        .get("server")
        .and_then(|server| server.get("web_read_origins"))
    else {
        return Origins::new();
    };
    let Some(values) = value.as_array() else {
        eprintln!("clarp-host: config.web_read_origins ignored: expected an array");
        return Origins::new();
    };
    let kept: Origins = values
        .iter()
        .filter_map(|value| normalize(value.as_str()?))
        .collect();
    if values
        .iter()
        .any(|value| value.as_str().and_then(normalize).is_none())
    {
        eprintln!("clarp-host: config.web_read_origins: invalid origins ignored");
    }
    kept
}

pub(crate) fn read_path(path: &str) -> bool {
    matches!(path, "/server-info" | "/agents/snapshot")
}

fn one<'a>(headers: &'a HeaderMap, key: &str) -> Option<&'a HeaderValue> {
    let mut values = headers.get_all(key).iter();
    let first = values.next()?;
    values.next().is_none().then_some(first)
}

fn listed<'a>(value: &'a HeaderValue, allowed: &Origins) -> Option<&'a str> {
    let text = value.to_str().ok().map(python::strip)?;
    // Configuration is normalized once. The header must already be a serialized origin.
    allowed.contains(text).then_some(text)
}

pub(crate) fn from_listed_origin(headers: &HeaderMap, allowed: &Origins) -> bool {
    headers
        .get_all(header::ORIGIN)
        .iter()
        .any(|value| listed(value, allowed).is_some())
}

pub(crate) fn response_origin(headers: &HeaderMap, allowed: &Origins) -> Option<HeaderValue> {
    let origin = listed(one(headers, "origin")?, allowed)?;
    HeaderValue::from_str(origin).ok()
}

pub(crate) fn allow(headers: &mut HeaderMap, origin: HeaderValue) {
    headers.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, origin);
    headers.append(header::VARY, HeaderValue::from_static("Origin"));
}

fn requested_headers(headers: &HeaderMap) -> bool {
    match one(headers, REQUEST_HEADERS) {
        None => !headers.contains_key(REQUEST_HEADERS),
        Some(value) => value.to_str().ok().is_some_and(|names| {
            names
                .split(',')
                .map(python::strip)
                .filter(|name| !name.is_empty())
                .all(|name| {
                    name.eq_ignore_ascii_case("authorization")
                        || name.eq_ignore_ascii_case("x-clarp-client")
                })
        }),
    }
}

pub(crate) fn preflight(path: &str, headers: &HeaderMap, allowed: &Origins) -> Response {
    let wanted_get = one(headers, REQUEST_METHOD)
        .and_then(|method| method.to_str().ok())
        .is_some_and(|method| python::strip(method) == "GET");
    let origin = if read_path(path) && wanted_get && requested_headers(headers) {
        response_origin(headers, allowed)
    } else {
        None
    };
    let mut response = match origin {
        Some(origin) => {
            let mut response = StatusCode::NO_CONTENT.into_response();
            let headers = response.headers_mut();
            allow(headers, origin);
            headers.insert(
                header::ACCESS_CONTROL_ALLOW_METHODS,
                HeaderValue::from_static("GET"),
            );
            headers.insert(
                header::ACCESS_CONTROL_ALLOW_HEADERS,
                HeaderValue::from_static("Authorization, X-Clarp-Client"),
            );
            headers.insert(
                header::ACCESS_CONTROL_MAX_AGE,
                HeaderValue::from_static("600"),
            );
            response
        }
        None => StatusCode::FORBIDDEN.into_response(),
    };
    response
        .headers_mut()
        .insert(header::CONTENT_TYPE, HeaderValue::from_static("text/plain"));
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origins_are_normalized_without_changing_the_host_or_accepting_credentials() {
        for (input, expected) in [
            (
                "HTTPS://Studio.Example.ts.net/",
                Some("https://studio.example.ts.net"),
            ),
            (
                "https://studio.example.ts.net:443",
                Some("https://studio.example.ts.net"),
            ),
            ("http://100.64.0.1:7682", Some("http://100.64.0.1:7682")),
            ("http://[::1]:7682", Some("http://[::1]:7682")),
            ("https://studio.example.ts.net/app", None),
            ("https://*.ts.net", None),
            ("null", None),
            ("ftp://studio", None),
            ("https://user:pw@studio", None),
            ("https://studio/?q=1", None),
            ("studio.example.ts.net", None),
            ("https://studio:65536", None),
            ("https://studio\\example", None),
        ] {
            assert_eq!(normalize(input).as_deref(), expected, "{input}");
        }
    }
}
