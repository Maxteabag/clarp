//! The owner-chosen recovery for a hosted engine that cannot hear a language set (R152).

use serde_json::{Value, json};

use super::{PROVIDERS, cloud_model, engine_order};
use crate::languages::SET_PROVIDERS;
use crate::python::strip;

pub const UNSUPPORTED: &str = "language_set_unsupported";
pub const OWN_UNAVAILABLE: &str = "the Host's own engine is unavailable";
pub const OWN_FAILED: &str = "the Host's own engine could not hear it";

/// The identity of an engine that refused or received the recording, for recovery.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Engine<'a> {
    Hosted(&'static str),
    Own,
    Other(&'a str),
}

pub fn identity<'a>(model: &'a str, own_id: Option<&str>) -> Engine<'a> {
    if let Some(provider) = engine_order::hosted_provider(model) {
        return Engine::Hosted(provider);
    }
    let name = strip(model);
    if engine_order::is_own(name, own_id) {
        Engine::Own
    } else {
        Engine::Other(name)
    }
}

/// The declaration of the Host's own engine. No declaration follows Whisper's .en rule.
#[derive(Clone, Copy, Debug)]
pub enum Own<'a> {
    Off,
    Unavailable,
    Model {
        id: &'a str,
        languages: &'a [&'a str],
    },
}

fn primary(code: &str) -> String {
    let code = strip(code);
    let code = code.split(['-', '_']).next().unwrap_or("").to_lowercase();
    match code.as_str() {
        "nb" | "nn" => "no".to_string(),
        _ => code,
    }
}

pub fn own_refusal(own: Own<'_>, languages: &[&str]) -> Option<String> {
    let (id, codes) = match own {
        Own::Off => return Some("the Host's own engine is off".into()),
        Own::Unavailable => return Some(OWN_UNAVAILABLE.into()),
        Own::Model { id, languages } => (id, languages),
    };
    let declared: Vec<String> = codes
        .iter()
        .map(|code| strip(code).to_lowercase())
        .filter(|code| !code.is_empty())
        .collect();
    let declared = if declared.is_empty() {
        vec![
            if id.ends_with(".en") {
                "en"
            } else {
                "multilingual"
            }
            .into(),
        ]
    } else {
        declared
    };
    if declared.iter().any(|code| code == "multilingual") {
        return None;
    }
    let heard: Vec<String> = declared.iter().map(|code| primary(code)).collect();
    let missing = languages.iter().any(|code| !heard.contains(&primary(code)));
    missing.then(|| {
        format!(
            "the Host's own model {id} hears only {}",
            declared.join(", ")
        )
    })
}

/// Pick only the one hosted engine the owner selected. Other providers' keys are irrelevant.
pub fn selected(model: &str, keyed: impl FnOnce(&str) -> bool) -> Option<&str> {
    let (provider, _) = cloud_model(model)?;
    (SET_PROVIDERS.contains(&provider.id) && keyed(provider.id)).then_some(model)
}

pub enum Fallback<'a> {
    Hosted(&'a str),
    Own(Vec<String>),
    Refused(Vec<String>),
}

/// Choose the second stage only after the ordinary order ended on an unsupported set.
pub fn fallback<'a>(
    rejected: Engine<'_>,
    candidate: Option<&'a str>,
    attempted: &[Engine<'_>],
    own: Own<'_>,
    languages: &[&str],
) -> Fallback<'a> {
    let mut reasons = Vec::new();
    if let Some(target) = candidate {
        let next = identity(target, None);
        if next == rejected || attempted.contains(&next) {
            reasons.push(format!("{target} already failed for this recording"));
        } else {
            return Fallback::Hosted(target);
        }
    }
    let why = own_refusal(own, languages).or_else(|| {
        attempted
            .contains(&Engine::Own)
            .then(|| "the Host's own engine already failed for this recording".into())
    });
    if let Some(why) = why {
        reasons.push(why);
        Fallback::Refused(reasons)
    } else {
        Fallback::Own(reasons)
    }
}

pub fn refusal(engine: &str, languages: &[&str], reasons: &[String]) -> Value {
    let engines: Vec<_> = PROVIDERS
        .iter()
        .filter(|provider| SET_PROVIDERS.contains(&provider.id))
        .flat_map(|provider| provider.models.iter().map(|model| model.id))
        .collect();
    let why = if reasons.is_empty() {
        String::new()
    } else {
        format!(" ({})", reasons.join("; "))
    };
    let message = format!(
        "{engine} cannot hear this agent's languages ({}); choose an engine that can: {}{why}",
        languages.join(", "),
        engines.join(", ")
    );
    json!({
        "error": UNSUPPORTED,
        "message": message,
        "engine": engine,
        "languages": languages,
        "engines": engines,
        "reasons": reasons,
    })
}

pub fn receipt(from: &str, to: &str, handed: Option<&str>) -> Value {
    let languages = match handed {
        None | Some("auto") => json!("detected"),
        Some(codes) => json!(codes.split(',').collect::<Vec<_>>()),
    };
    json!({"from": from, "to": to, "reason": "language_set", "languages": languages})
}
