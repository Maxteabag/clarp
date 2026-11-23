//! How a failure Antigravity reports is read, from the words it sends: a spent quota and when it
//! resets, a delay to wait, a conversation that cannot be continued, a missing sign-in, and an
//! error of Google's API. Apart from what reads the environment, so that the words of its errors
//! are never taken for names of variables.

use serde_json::Value;

/// What went wrong, as Antigravity's own words say it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Failure {
    /// The account's quota is spent. `resets`: when it begins anew, as Antigravity says it (a
    /// moment) or how long until then (`in 3h2m`), where it says either.
    Quota {
        resets: Option<String>,
    },
    /// The provider asked to be tried again later, after this long.
    Retry {
        after: String,
    },
    /// The conversation holds a thinking block without a valid signature: every later prompt
    /// of it fails the same way.
    Signature,
    /// The conversation is not known to Antigravity.
    Unknown,
    /// Antigravity is not signed in.
    SignedOut,
    /// An error of Google's API, with its code and status where they are said.
    Api {
        code: String,
        status: String,
        message: String,
    },
    Other,
}

/// The value written after `key` (`key:value`), up to a blank, a bracket, a comma or a quote.
fn field<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    let at = text.find(key)? + key.len();
    let rest = text[at..].trim_start_matches([' ', '"']);
    let end = rest
        .find(|c: char| c.is_whitespace() || matches!(c, ']' | ',' | '"' | '}' | ')'))
        .unwrap_or(rest.len());
    Some(&rest[..end]).filter(|value| !value.is_empty())
}

/// Whether a text is a duration as Go writes one (`1h2m3.5s`, `250ms`).
fn duration(text: &str) -> bool {
    let mut rest = text;
    let mut any = false;
    while !rest.is_empty() {
        let digits = rest
            .find(|c: char| !(c.is_ascii_digit() || c == '.'))
            .unwrap_or(rest.len());
        if digits == 0 {
            return false;
        }
        rest = &rest[digits..];
        let Some(unit) = ["ns", "us", "µs", "μs", "ms", "h", "m", "s"]
            .into_iter()
            .find(|unit| rest.starts_with(unit))
        else {
            return false;
        };
        rest = &rest[unit.len()..];
        any = true;
    }
    any
}

/// `Error 429, Message: …, Status: RESOURCE_EXHAUSTED`: Google's error as the agent's
/// harness writes it.
fn api_error(text: &str) -> Option<Failure> {
    let mut from = 0;
    while let Some(found) = text[from..].find("Error ") {
        let at = from + found + "Error ".len();
        from = at;
        let Some(code) = text.get(at..at + 3) else {
            continue;
        };
        if !code.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        let Some(rest) = text[at + 3..].strip_prefix(", Message: ") else {
            continue;
        };
        let Some((message, after)) = rest.split_once(", Status: ") else {
            continue;
        };
        let status: String = after
            .chars()
            .take_while(|c| c.is_ascii_uppercase() || *c == '_' || c.is_ascii_digit())
            .collect();
        return Some(Failure::Api {
            code: code.to_string(),
            status,
            message: message.trim().to_string(),
        });
    }
    None
}

/// `RESOURCE_EXHAUSTED (code 429): message`: the same error as Antigravity 1.5 writes it for
/// a Google account.
fn status_error(text: &str) -> Option<Failure> {
    let at = text.find(" (code ")?;
    let status: String = text[..at]
        .chars()
        .rev()
        .take_while(|c| c.is_ascii_uppercase() || *c == '_')
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    let rest = &text[at + " (code ".len()..];
    let code = rest.get(..3)?;
    let message = rest[3..].strip_prefix("): ")?;
    let message = message.lines().next().unwrap_or_default();
    (!status.is_empty() && code.bytes().all(|b| b.is_ascii_digit())).then(|| Failure::Api {
        code: code.to_string(),
        status,
        message: message.trim().to_string(),
    })
}

/// What a failure Antigravity reported is, from what it said: the error of a request, the
/// lines it wrote to its standard error, or both.
pub fn classify(text: &str) -> Failure {
    let lower = text.to_ascii_lowercase();
    if lower.contains("thinking.signature: field required")
        || lower.contains("invalid `signature` in `thinking` block")
        || lower.contains("invalid_thinking_signature")
    {
        return Failure::Signature;
    }
    if text.contains("QUOTA_EXHAUSTED") {
        let at = field(text, "quotaResetTimeStamp:").map(str::to_string);
        let delay = field(text, "quotaResetDelay:")
            .filter(|delay| duration(delay))
            .map(|delay| format!("in {delay}"));
        return Failure::Quota {
            resets: at.or(delay),
        };
    }
    if let Some(after) = field(text, "retryDelay:").filter(|delay| duration(delay)) {
        return Failure::Retry {
            after: after.to_string(),
        };
    }
    if lower.contains("authentication required")
        || lower.contains("not authenticated")
        || lower.contains("not logged in")
        || lower.contains("please log in")
        || lower.contains("auth_required")
    {
        return Failure::SignedOut;
    }
    if let Some(api) = api_error(text).or_else(|| status_error(text)) {
        return api;
    }
    if lower.contains("session not found")
        || lower.contains("conversation not found")
        || lower.contains("unknown session")
        || lower.contains("no such session")
    {
        return Failure::Unknown;
    }
    Failure::Other
}

/// What the user is told of a failure: in words the Host's own reading of failures knows
/// (a spent quota reads as a usage limit, a sign-in as one that is missing).
pub fn reason(failure: &Failure, said: &str) -> String {
    let said = said.trim();
    match failure {
        Failure::Quota { resets: Some(at) } if at.starts_with("in ") => {
            format!("Antigravity usage quota exhausted; it resets {at}.")
        }
        Failure::Quota { resets: Some(at) } => {
            format!("Antigravity usage quota exhausted; it resets at {at}.")
        }
        Failure::Quota { resets: None } => "Antigravity usage quota exhausted.".to_string(),
        Failure::Retry { after } => {
            format!("Antigravity was rate limited by Google; try again in {after}.")
        }
        Failure::Signature => "Antigravity's conversation holds a thinking block without a valid \
                               signature, so it cannot be continued; the next turn starts a new \
                               conversation with this one's record."
            .to_string(),
        Failure::Unknown => format!("Antigravity no longer has this conversation: {said}"),
        Failure::SignedOut => format!(
            "Antigravity is not logged in on this Host; sign in by running agy on the Host. ({said})"
        ),
        Failure::Api {
            code,
            status,
            message,
        } => format!("Google API error {code} {status}: {message}"),
        Failure::Other if said.is_empty() => "Antigravity's turn failed.".to_string(),
        Failure::Other => said.to_string(),
    }
}

/// The text of a JSON-RPC error: its message, and what its data says.
pub fn error_text(error: &Value) -> String {
    let message = error["message"].as_str().unwrap_or_default();
    let data = match &error["data"] {
        Value::Null => String::new(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    };
    match (message.is_empty(), data.is_empty()) {
        (_, true) => message.to_string(),
        (true, false) => data,
        (false, false) => format!("{message}: {data}"),
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn a_spent_quota_is_read_with_its_reset() {
        let said = "Error 429, Message: Resource has been exhausted, Status: RESOURCE_EXHAUSTED \
                    [reason:QUOTA_EXHAUSTED quotaResetTimeStamp:2026-11-23T18:00:00Z]";
        assert_eq!(
            classify(said),
            Failure::Quota {
                resets: Some("2026-11-23T18:00:00Z".into())
            }
        );
        let delayed = "RESOURCE_EXHAUSTED (code 429): quota [reason:QUOTA_EXHAUSTED quotaResetDelay:3h2m1.5s]";
        assert_eq!(
            classify(delayed),
            Failure::Quota {
                resets: Some("in 3h2m1.5s".into())
            }
        );
        let text = reason(&classify(said), said);
        assert!(text.contains("usage quota exhausted") && text.contains("resets at"));
        assert_eq!(
            classify("reason:QUOTA_EXHAUSTED quotaResetDelay:soon"),
            Failure::Quota { resets: None }
        );
    }

    #[test]
    fn a_retry_delay_a_signature_and_api_errors_are_told_apart() {
        assert_eq!(
            classify("Error 429, Message: slow down, Status: RESOURCE_EXHAUSTED [retryDelay:30s]"),
            Failure::Retry {
                after: "30s".into()
            }
        );
        for said in [
            "messages.3.content.0.thinking.signature: Field required",
            "messages.12.content.1: Invalid `signature` in `thinking` block",
        ] {
            assert_eq!(classify(said), Failure::Signature, "{said}");
        }
        assert_eq!(
            classify(
                "Error 503, Message: The model is overloaded, Status: UNAVAILABLE\ngoroutine 1"
            ),
            Failure::Api {
                code: "503".into(),
                status: "UNAVAILABLE".into(),
                message: "The model is overloaded".into()
            }
        );
        assert_eq!(
            classify("rpc: PERMISSION_DENIED (code 403): caller lacks access\nstack"),
            Failure::Api {
                code: "403".into(),
                status: "PERMISSION_DENIED".into(),
                message: "caller lacks access".into()
            }
        );
        assert_eq!(classify("Authentication required"), Failure::SignedOut);
        assert_eq!(classify("Session not found: abc"), Failure::Unknown);
        assert_eq!(classify("something else"), Failure::Other);
    }
}
