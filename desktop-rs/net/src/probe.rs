//! One quiet look at whether a Host answers (`GET /server-info` with the
//! device token and a short timeout), and how it failed when it did not,
//! for the "Host not answering" panel. Each probe has its own connection:
//! a Host that hangs never holds up another's.

use std::time::Duration;

use clarp_core::host_down::Attempt;
use serde_json::Value;

use crate::USER_AGENT;
use crate::api::auth_headers;

/// What a probe found.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Probe {
    /// This Host answered: its id, version and contract.
    Answered { server_id: String, version: String, contract: Option<u32> },
    Failed(Attempt),
}

/// Asks `base`'s `/server-info` with `token`, waiting at most `timeout`.
/// `expected_server_id` (when known) must match the answer, else something
/// else lives at the address now.
pub async fn probe(base: &str, token: &str, expected_server_id: &str, timeout: Duration) -> Probe {
    let Some(url) = url::Url::parse(base).ok().and_then(|base| clarp_core::endpoint::resolve(&clarp_core::endpoint::normalize_base(base), "/server-info"))
    else {
        return Probe::Failed(Attempt::NameNotFound);
    };
    let client = match reqwest::Client::builder()
        .user_agent(USER_AGENT)
        .connect_timeout(timeout)
        .timeout(timeout)
        .redirect(reqwest::redirect::Policy::none())
        .build()
    {
        Ok(client) => client,
        Err(_) => return Probe::Failed(Attempt::Other),
    };
    let response = match client.get(url).headers(auth_headers(token, "application/json")).send().await {
        Ok(response) => response,
        Err(error) => return Probe::Failed(attempt_of(&error, timeout)),
    };
    let status = response.status().as_u16();
    if !(200..300).contains(&status) {
        return Probe::Failed(Attempt::Http(status));
    }
    let body = match response.bytes().await {
        Ok(body) => body,
        Err(error) => return Probe::Failed(attempt_of(&error, timeout)),
    };
    answered(&body, expected_server_id)
}

/// A 2xx body: this Host's identity, or `NotThisHost` when it is not a
/// Clarp Host's or names another Host.
pub fn answered(body: &[u8], expected_server_id: &str) -> Probe {
    let Ok(Value::Object(info)) = serde_json::from_slice::<Value>(body) else { return Probe::Failed(Attempt::NotThisHost) };
    let text = |key: &str| info.get(key).and_then(Value::as_str).unwrap_or_default().to_owned();
    let server_id = text("server_id");
    if !same_host(&info, expected_server_id) {
        return Probe::Failed(Attempt::NotThisHost);
    }
    let contract = info.get("contract").and_then(|c| c.get("host").or(Some(c))).and_then(Value::as_u64).and_then(|c| u32::try_from(c).ok());
    Probe::Answered { server_id, version: text("clarp_version"), contract }
}

/// A moved Host may retain its original pairing under a previous server ID.
pub fn same_host(info: &clarp_core::json::Object, expected: &str) -> bool {
    let Some(id) = info.get("server_id").and_then(Value::as_str).filter(|s| !s.is_empty()) else { return false };
    expected.is_empty() || id == expected || info.get("previous_server_ids").and_then(Value::as_array)
        .is_some_and(|ids| ids.iter().any(|id| id.as_str() == Some(expected)))
}

/// How a request failed, from the error and its causes.
fn attempt_of(error: &reqwest::Error, timeout: Duration) -> Attempt {
    if error.is_timeout() {
        return Attempt::Timeout(timeout);
    }
    let mut source: Option<&(dyn std::error::Error + 'static)> = Some(error);
    let mut words = Vec::new();
    while let Some(cause) = source {
        if let Some(io) = cause.downcast_ref::<std::io::Error>() {
            match io.kind() {
                std::io::ErrorKind::ConnectionRefused => return Attempt::Refused,
                std::io::ErrorKind::TimedOut => return Attempt::Timeout(timeout),
                std::io::ErrorKind::HostUnreachable | std::io::ErrorKind::NetworkUnreachable => return Attempt::NoRoute,
                _ => {}
            }
        }
        words.push(cause.to_string().to_ascii_lowercase());
        source = cause.source();
    }
    attempt_of_words(&words.join(" | "), timeout)
}

/// The causes' words, where no typed error said it.
pub fn attempt_of_words(words: &str, timeout: Duration) -> Attempt {
    let words = words.to_ascii_lowercase();
    if words.contains("connection refused") {
        Attempt::Refused
    } else if words.contains("dns error") || words.contains("failed to lookup address") || words.contains("name or service not known") || words.contains("nodename nor servname") || words.contains("no such host") {
        Attempt::NameNotFound
    } else if words.contains("no route to host") || words.contains("network is unreachable") || words.contains("host is unreachable") {
        Attempt::NoRoute
    } else if words.contains("certificate") || words.contains("tls") || words.contains("handshake") {
        Attempt::Tls
    } else if words.contains("timed out") {
        Attempt::Timeout(timeout)
    } else {
        Attempt::Other
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_answer_is_this_host_or_not() {
        let info = br#"{"server_id":"srv-1","clarp_version":"2.4.0","contract":{"host":51,"min_ios":3,"features":{}}}"#;
        assert_eq!(answered(info, "srv-1"), Probe::Answered { server_id: "srv-1".into(), version: "2.4.0".into(), contract: Some(51) });
        assert_eq!(answered(info, ""), Probe::Answered { server_id: "srv-1".into(), version: "2.4.0".into(), contract: Some(51) });
        assert_eq!(answered(info, "srv-2"), Probe::Failed(Attempt::NotThisHost));
        let renamed = br#"{"server_id":"srv-3","previous_server_ids":["srv-1"]}"#;
        assert!(matches!(answered(renamed, "srv-1"), Probe::Answered { .. }));
        assert_eq!(answered(b"<html>router login</html>", ""), Probe::Failed(Attempt::NotThisHost));
        assert_eq!(answered(br#"{"status":"ok"}"#, ""), Probe::Failed(Attempt::NotThisHost));
    }

    const T: Duration = Duration::from_secs(8);

    #[test]
    fn failures_are_named_from_their_words() {
        assert_eq!(attempt_of_words("error sending request | client error (Connect) | tcp connect error | Connection refused (os error 111)", T), Attempt::Refused);
        assert_eq!(attempt_of_words("dns error: failed to lookup address information: Name or service not known", T), Attempt::NameNotFound);
        assert_eq!(attempt_of_words("tcp connect error: No route to host (os error 113)", T), Attempt::NoRoute);
        assert_eq!(attempt_of_words("invalid peer certificate: NotValidForName", T), Attempt::Tls);
        assert_eq!(attempt_of_words("something odd", T), Attempt::Other);
    }

    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap()
    }

    #[test]
    fn a_closed_port_is_refused_and_a_bogus_name_not_found() {
        let rt = runtime();
        let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = closed.local_addr().unwrap();
        drop(closed);
        let refused = rt.block_on(probe(&format!("http://{address}"), "t", "", Duration::from_secs(2)));
        assert_eq!(refused, Probe::Failed(Attempt::Refused));
        let bogus = rt.block_on(probe("http://no-such-host.invalid:7690", "t", "", Duration::from_secs(4)));
        assert_eq!(bogus, Probe::Failed(Attempt::NameNotFound));
    }

    #[test]
    fn a_host_that_never_answers_times_out_in_time() {
        let rt = runtime();
        let hung = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let started = std::time::Instant::now();
        let outcome = rt.block_on(probe(&format!("http://{}", hung.local_addr().unwrap()), "t", "", Duration::from_millis(500)));
        assert_eq!(outcome, Probe::Failed(Attempt::Timeout(Duration::from_millis(500))));
        assert!(started.elapsed() < Duration::from_secs(3));
    }
}
