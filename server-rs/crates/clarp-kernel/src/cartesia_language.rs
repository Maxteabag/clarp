//! R168: the first hearing language, constrained to what the Cartesia model speaks.

use crate::languages::{of_agent, primary};
use crate::python::strip;
use crate::said::Who;

const SONIC_3: &[&str] = &[
    "en", "de", "es", "fr", "ja", "pt", "zh", "hi", "ko", "it", "nl", "pl", "ru", "sv", "tr", "tl",
    "bg", "ro", "ar", "cs", "el", "fi", "hr", "ms", "sk", "da", "ta", "uk", "hu", "no", "vi", "bn",
    "th", "he", "ka", "id", "te", "gu", "kn", "ml", "mr", "pa",
];
const SONIC_2: &[&str] = &["en", "fr", "de", "es", "pt", "zh", "ja", "ko"];
const SONIC_2024: &[&str] = &[
    "en", "fr", "de", "es", "pt", "zh", "ja", "hi", "it", "ko", "nl", "pl", "ru", "sv", "tr",
];

/// Language declarations for a model family, including dated snapshots.
pub fn model_languages(model: &str) -> impl Iterator<Item = &'static str> {
    let model = strip(model).to_lowercase();
    let (base, additions): (&[&str], &[&str]) =
        if model == "sonic-latest" || model.starts_with("sonic-3.6") {
            (SONIC_3, &["or", "ur"])
        } else if model.starts_with("sonic-3") {
            (SONIC_3, &[])
        } else if model == "sonic" || model.starts_with("sonic-2024") {
            (SONIC_2024, &[])
        } else if model.starts_with("sonic-2") {
            (SONIC_2, &[])
        } else if model.starts_with("sonic-turbo") {
            (SONIC_2, &["hi"])
        } else {
            (&["en"], &[])
        };
    base.iter().chain(additions).copied()
}

/// If the model limits the language, retain the primary code for the request's log.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Language {
    pub code: &'static str,
    pub unsupported: Option<String>,
}

pub fn language_for(code: &str, model: &str) -> Language {
    let primary = primary(code);
    if let Some(code) = model_languages(model).find(|&language| language == primary) {
        return Language {
            code,
            unsupported: None,
        };
    }
    Language {
        code: "en",
        unsupported: (!matches!(primary.as_str(), "" | "en")).then_some(primary),
    }
}

pub fn agent_speech_language<'a>(
    overrides: &'a [(String, String)],
    who: Option<Who<'_>>,
    default: &'a str,
) -> &'a str {
    let language = who
        .and_then(|who| of_agent(overrides, who))
        .filter(|value| !value.is_empty())
        .unwrap_or(default);
    match strip(language.split(',').next().unwrap_or("")) {
        "" => "en",
        first => first,
    }
}
