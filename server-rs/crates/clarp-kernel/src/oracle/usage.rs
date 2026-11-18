//! Replacement snapshots from a Live voice, independent of the router's charges.
use serde_json::{Value, json};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Billing {
    Api,
    Subscription,
    Gemini,
}

#[derive(Clone, Debug)]
pub struct LiveUsage {
    billing: Billing,
    seconds: Value,
    ratio: Value,
    expires: Value,
    estimate: Value,
    reason: Option<String>,
    usage_final: bool,
}

impl LiveUsage {
    pub fn new(billing: Billing) -> Self {
        Self {
            billing,
            seconds: Value::Null,
            ratio: Value::Null,
            expires: Value::Null,
            estimate: Value::Null,
            reason: None,
            usage_final: false,
        }
    }

    pub fn observe(&mut self, event: &Value) -> bool {
        let kind = event["type"].as_str().unwrap_or("");
        if self.reason.is_some()
            || !matches!(
                kind,
                "session.started" | "session.closed" | "session.usage.updated"
            )
        {
            return false;
        }
        let number = |v: &Value| v.as_f64().is_some_and(|n| n.is_finite() && n >= 0.);
        let seconds = &event["usage"]["seconds"];
        let estimate = &event["usage"]["voice_estimate_usd"];
        let expires = &event["session"]["expires_at"];
        let ratio = &event["context_window"]["usage_ratio"];
        if number(seconds) {
            self.seconds = seconds.clone();
        }
        if number(estimate) && self.billing == Billing::Gemini {
            self.estimate = estimate.clone();
        }
        if number(expires) {
            self.expires = expires.clone();
        }
        if number(ratio) && ratio.as_f64() <= Some(1.) {
            self.ratio = ratio.clone();
        }
        if kind == "session.closed" {
            let reason = super::said(&event["reason"]);
            self.reason = Some(if reason.is_empty() {
                "unknown".into()
            } else {
                reason.chars().take(120).collect()
            });
            self.usage_final = number(seconds);
        }
        true
    }

    pub fn snapshot(&self) -> Value {
        let (name, estimate) = match self.billing {
            Billing::Api => (
                "openai_api",
                json!(self.seconds.as_f64().map(|n| round(n * 0.05 / 60., 6))),
            ),
            Billing::Subscription => ("chatgpt_subscription", Value::Null),
            Billing::Gemini => ("gemini_api", self.estimate.clone()),
        };
        json!({"seconds": self.seconds, "context_ratio": (self.billing != Billing::Gemini).then_some(&self.ratio),
            "expires_at": (self.billing != Billing::Gemini).then_some(&self.expires),
            "finalized": self.reason.is_some(), "usage_final": self.usage_final, "reason": self.reason,
            "voice_estimate_usd": estimate, "billing": name, "scope": "voice_only"})
    }
}

pub(super) fn round(n: f64, digits: usize) -> f64 {
    format!("{n:.digits$}").parse().unwrap_or(n)
}
