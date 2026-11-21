//! Engine identity and the providers that heard an attempt for one recording (R175).

use std::collections::{HashSet, VecDeque};

use super::{PROVIDERS, provider_of};
use crate::python::strip;

pub(super) fn is_own(name: &str, own_model: Option<&str>) -> bool {
    matches!(name, "" | "server-default" | "local") || own_model == Some(name)
}

pub fn identity<'a>(engine: &'a str, own_model: Option<&str>) -> &'a str {
    let name = strip(engine);
    if is_own(name, own_model) { "own" } else { name }
}

/// Keep the first name of each engine, and report the repeated names in the same order.
/// Different models of one provider are distinct until an attempt reaches that provider.
pub fn distinct_engines<'a>(
    order: impl IntoIterator<Item = &'a str>,
    own_model: Option<&str>,
) -> (Vec<&'a str>, Vec<&'a str>) {
    let mut seen = HashSet::new();
    order
        .into_iter()
        .partition(|engine| seen.insert(identity(engine, own_model)))
}

pub fn hosted_provider(engine: &str) -> Option<&'static str> {
    let provider = provider_of(strip(engine));
    PROVIDERS
        .iter()
        .find(|known| known.id == provider)
        .map(|known| known.id)
}

/// A clip's provider contacts. A fresh clip always starts with an empty set.
#[derive(Default)]
pub struct Contacts {
    sent: HashSet<&'static str>,
}

impl Contacts {
    /// Remember a provider after its adapter received the attempt. A local failure counts
    /// for R152, but it cannot exhaust a hosted provider.
    pub fn failed(&mut self, engine: &str, handed: bool) -> bool {
        let Some(provider) = hosted_provider(engine) else {
            return true;
        };
        if handed {
            self.sent.insert(provider);
        }
        handed
    }

    pub fn already_sent(&self, engine: &str) -> Option<&'static str> {
        hosted_provider(engine).filter(|provider| self.sent.contains(provider))
    }
}

/// The ordinary chain loses a contacted provider's later models as soon as an attempt fails.
pub struct ChatOrder<'a> {
    pending: VecDeque<&'a str>,
    contacted: Contacts,
}

pub struct Failure<'a> {
    pub reached: bool,
    pub has_fallback: bool,
    pub skipped: Vec<(&'a str, &'static str)>,
}

impl<'a> ChatOrder<'a> {
    pub fn new(order: Vec<&'a str>) -> Self {
        Self {
            pending: order.into(),
            contacted: Contacts::default(),
        }
    }

    pub fn attempt(&mut self) -> Option<(&'a str, bool)> {
        let engine = self.pending.pop_front()?;
        Some((engine, !self.pending.is_empty()))
    }

    pub fn failed(&mut self, engine: &str, handed: bool) -> Failure<'a> {
        let reached = self.contacted.failed(engine, handed);
        let mut skipped = Vec::new();
        self.pending.retain(|later| {
            if let Some(provider) = self.contacted.already_sent(later) {
                skipped.push((*later, provider));
                false
            } else {
                true
            }
        });
        Failure {
            reached,
            has_fallback: !self.pending.is_empty(),
            skipped,
        }
    }
}

pub fn listed_before(focus: &str, engine: &str) -> String {
    format!("session={} {engine} (listed before)", session(focus))
}

pub fn provider_already_had(focus: &str, engine: &str, provider: &str) -> String {
    format!(
        "session={} {engine} ({provider} already had this recording)",
        session(focus)
    )
}

fn session(focus: &str) -> &str {
    if focus.is_empty() { "-" } else { focus }
}
