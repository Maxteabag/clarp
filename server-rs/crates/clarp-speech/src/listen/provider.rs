//! One audio send and the transport's proof that it could not have left.

use std::fmt;
use std::io::ErrorKind;
use std::time::Duration;

use ureq::config::Config;
use ureq::http::Uri;
use ureq::unversioned::resolver::{DefaultResolver, ResolvedSocketAddrs, Resolver};
use ureq::unversioned::transport::{DefaultConnector, NextTimeout};

use super::Observation;

#[derive(Debug)]
struct ResolveFailed(ureq::Error);

impl fmt::Display for ResolveFailed {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(&self.0, f)
    }
}

impl std::error::Error for ResolveFailed {}

#[derive(Debug, Default)]
struct ProviderResolver(DefaultResolver);

impl Resolver for ProviderResolver {
    fn resolve(
        &self,
        uri: &Uri,
        config: &Config,
        timeout: NextTimeout,
    ) -> Result<ResolvedSocketAddrs, ureq::Error> {
        // The standard resolver can return an undifferentiated I/O failure for DNS.
        // Keep the diagnostic and identify its phase before the transport sees it.
        self.0
            .resolve(uri, config, timeout)
            .map_err(|error| ureq::Error::Other(Box::new(ResolveFailed(error))))
    }
}

pub(crate) fn http_agent(timeout: Duration) -> ureq::Agent {
    let config = ureq::Agent::config_builder()
        .max_redirects(0)
        .timeout_global(Some(timeout))
        .http_status_as_error(false)
        .build();
    // A redirect would put an earlier send and a later connection failure in one result.
    ureq::Agent::with_parts(
        config,
        DefaultConnector::default(),
        ProviderResolver::default(),
    )
}

impl Observation {
    pub(crate) fn send_http<T>(
        &self,
        send: impl FnOnce() -> Result<T, ureq::Error>,
    ) -> Result<T, ureq::Error> {
        self.sending_audio(send, |error| match error {
            ureq::Error::HostNotFound => true,
            ureq::Error::Io(error) => error.kind() == ErrorKind::ConnectionRefused,
            ureq::Error::Other(error) => error.is::<ResolveFailed>(),
            _ => false,
        })
    }

    pub(crate) fn send_socket(
        &self,
        send: impl FnOnce() -> Result<(), tungstenite::Error>,
    ) -> Result<(), tungstenite::Error> {
        self.sending_audio(send, |error| {
            matches!(error, tungstenite::Error::Io(error) if error.kind() == ErrorKind::ConnectionRefused)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};
    use std::sync::atomic::Ordering;

    #[test]
    fn a_later_pre_send_failure_does_not_erase_an_earlier_send() {
        let observation = Observation::default();
        let not_resolved = || {
            ureq::Error::Other(Box::new(ResolveFailed(ureq::Error::Io(
                std::io::Error::other("resolver detail"),
            ))))
        };
        for error in [
            ureq::Error::HostNotFound,
            not_resolved(),
            ureq::Error::Io(ErrorKind::ConnectionRefused.into()),
        ] {
            assert!(observation.send_http(|| Err::<(), _>(error)).is_err());
            assert!(!observation.sent());
        }
        // A timeout after the request starts is ambiguous. Once any piece might have
        // left, a failed resolution on a later piece cannot reopen the provider.
        let unknown = ureq::Error::Io(ErrorKind::TimedOut.into());
        assert!(observation.send_http(|| Err::<(), _>(unknown)).is_err());
        assert!(
            observation
                .send_http(|| Err::<(), _>(not_resolved()))
                .is_err()
        );
        assert!(observation.sent());
    }

    #[test]
    #[allow(clippy::unwrap_used)]
    fn overlapping_sends_and_refusals_match_the_reference_count() {
        let vectors: Value = serde_json::from_str(include_str!(
            "../../../clarp-kernel/tests/fixtures/provider-once-vectors.json"
        ))
        .unwrap();
        let outcome = |name: &Value| match name.as_str().unwrap() {
            "ok" => Ok(()),
            "refused" => Err(ureq::Error::Io(ErrorKind::ConnectionRefused.into())),
            "dns" => Err(ureq::Error::HostNotFound),
            "timeout" => Err(ureq::Error::Io(ErrorKind::TimedOut.into())),
            other => panic!("unknown fixture outcome {other}"),
        };
        for case in vectors["send_counts"].as_array().unwrap() {
            let observed = Observation::default();
            let read = || observed.sends.load(Ordering::Relaxed);
            let mut counts = vec![read()];
            let _ = observed.send_http(|| {
                counts.push(read());
                let _ = observed.send_http(|| {
                    counts.push(read());
                    outcome(&case["inner"])
                });
                counts.push(read());
                outcome(&case["outer"])
            });
            counts.push(read());
            assert_eq!(json!(counts), case["counts"], "{case}");
            assert_eq!(json!(observed.sent()), case["sent"], "{case}");
        }
    }
}
