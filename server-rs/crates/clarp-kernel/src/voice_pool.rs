//! The deterministic voice assignment for a new agent or contact (V2b).

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::python::{casefold, sorted_ascii, strip};
use crate::said::{self, CARTESIA, ELEVENLABS, GEMINI, Route, Who};
use crate::voice_profile::{self, Agent, HostView};
use crate::{cartesia_language, languages, voice_names::Snapshot};

pub struct Pool<'a> {
    pub host: &'a HostView,
    pub configured_cartesia: &'a [String],
    pub catalogue: &'a Snapshot,
    pub roster: &'a [(String, String)],
}

/// Active agents and custom contacts are distinguished from the built-in contacts.
pub struct User<'a> {
    pub name: &'a str,
    pub stored: &'a str,
    pub builtin: bool,
}

pub struct Exhausted {
    pub provider: &'static str,
    pub size: usize,
    pub shared_by: usize,
}

pub struct Assignment {
    pub stored: String,
    pub exhausted: Vec<Exhausted>,
}

const PROVIDERS: [&str; 3] = [CARTESIA, ELEVENLABS, GEMINI];

fn canonical(stored: &str) -> Map<String, Value> {
    said::voice_map(Some(&said::canonical_voice(stored)))
        .into_iter()
        .map(|(provider, id)| (provider, Value::String(id)))
        .collect()
}

/// Voices that can be assigned from today's Cartesia catalogue.
pub fn language_voices(snapshot: &Snapshot, language: &str) -> Vec<String> {
    let language = languages::primary(language);
    let gone = snapshot.gone.get(CARTESIA);
    snapshot
        .catalogue
        .get(CARTESIA)
        .into_iter()
        .flatten()
        .filter(|(id, row)| {
            row.public
                && !row.owner
                && !gone.is_some_and(|gone| gone.contains(*id))
                && languages::primary(&row.language) == language
        })
        .map(|(id, _)| id.clone())
        .collect()
}

impl Pool<'_> {
    fn needed(&self, name: &str) -> Vec<&'static str> {
        let voices = &self.host.voices;
        let who = Who {
            persona: name,
            ..Who::default()
        };
        let override_provider = voices
            .entry_for(who)
            .iter()
            .rev()
            .find(|(key, _)| key == "provider")
            .map_or("", |(_, provider)| provider.as_str());
        let mut providers = Vec::new();
        for provider in [
            voices.provider.as_str(),
            &voices.fallback,
            override_provider,
        ] {
            if let Some(provider) = PROVIDERS.iter().copied().find(|known| *known == provider)
                && !providers.contains(&provider)
            {
                providers.push(provider);
            }
        }
        providers
    }

    pub fn candidates(&self, provider: &str, name: &str) -> Vec<String> {
        if provider != CARTESIA {
            return crate::voices::pool_ids(provider);
        }
        let mut seen = BTreeSet::new();
        let source = if self.configured_cartesia.is_empty() {
            let who = Who {
                name,
                persona: name,
                ..Who::default()
            };
            let language = cartesia_language::agent_speech_language(
                &self.host.agent_languages,
                Some(who),
                &self.host.language,
            );
            let live = language_voices(self.catalogue, language);
            if live.is_empty() {
                said::BUILT_IN_CARTESIA_VOICES
                    .iter()
                    .map(|(_, id)| (*id).into())
                    .collect()
            } else {
                live
            }
        } else {
            self.configured_cartesia.to_vec()
        };
        source
            .into_iter()
            .filter(|id| !id.is_empty() && seen.insert(id.clone()))
            .collect()
    }

    fn reserves(&self, provider: &str) -> BTreeSet<String> {
        let from: &[(String, String)] = match provider {
            CARTESIA => &self.host.voices.cartesia_voices,
            GEMINI => &self.host.voices.gemini_voices,
            ELEVENLABS => self.roster,
            _ => return BTreeSet::new(),
        };
        from.iter()
            .filter(|(_, id)| !id.is_empty())
            .map(|(_, id)| id.clone())
            .collect()
    }

    fn speaks(&self, stored: &str, name: &str, provider: &str) -> Option<String> {
        let who = Who {
            persona: name,
            ..Who::default()
        };
        let agent = Agent {
            who,
            stored: Some(stored),
        };
        let route = Route {
            provider: String::new(),
            fallback: String::new(),
            voice: None,
        };
        let choice = voice_profile::provider_voice(self.host, agent, &route, provider);
        if choice.origin != Some(voice_profile::OWN)
            || (provider == ELEVENLABS
                && !voice_profile::elevenlabs_id_usable(choice.voice.as_deref()))
        {
            return None;
        }
        choice.voice
    }

    pub fn assign(&self, name: &str, stored: &str, users: &[User<'_>]) -> Assignment {
        let mut mapping = canonical(stored);
        let without_placeholders = mapping.len();
        mapping.retain(|_, id| !id.as_str().is_some_and(|id| id.starts_with("auto-")));
        let raw = sorted_ascii(&Value::Object(mapping.clone()));
        let missing: Vec<_> = self
            .needed(name)
            .into_iter()
            .filter(|provider| self.speaks(&raw, name, provider).is_none())
            .collect();
        if missing.is_empty() {
            let kept = if mapping.len() == without_placeholders {
                stored.to_owned()
            } else {
                raw
            };
            return Assignment {
                stored: kept,
                exhausted: Vec::new(),
            };
        }

        let mut exhausted = Vec::new();
        for provider in missing {
            let candidates = self.candidates(provider, name);
            let mut reserved = self.reserves(provider);
            let mut active = BTreeMap::<String, usize>::new();
            let mut taken = BTreeMap::<String, usize>::new();
            for user in users {
                if let Some(voice) = self.speaks(user.stored, user.name, provider) {
                    *taken.entry(voice.clone()).or_default() += 1;
                    if user.builtin {
                        reserved.insert(voice);
                    } else {
                        *active.entry(voice).or_default() += 1;
                    }
                }
            }
            if let Some(picked) = pick(name, &candidates, &taken, &reserved) {
                let shared_by = taken.get(picked).copied().unwrap_or_default();
                if shared_by > 0 && candidates.iter().all(|id| active.contains_key(id)) {
                    exhausted.push(Exhausted {
                        provider,
                        size: candidates.len(),
                        shared_by,
                    });
                }
                mapping.insert(provider.to_owned(), Value::String(picked.to_owned()));
            }
        }
        Assignment {
            stored: sorted_ascii(&Value::Object(mapping)),
            exhausted,
        }
    }

    pub fn pinned_before_rename(&self, name: &str, stored: &str) -> String {
        let mut mapping = canonical(stored);
        let raw = sorted_ascii(&Value::Object(mapping.clone()));
        for provider in self.needed(name) {
            if !mapping.contains_key(provider)
                && let Some(voice) = self.speaks(&raw, name, provider)
            {
                mapping.insert(provider.into(), Value::String(voice));
            }
        }
        sorted_ascii(&Value::Object(mapping))
    }
}

pub fn pick<'a>(
    name: &str,
    candidates: &'a [String],
    taken: &BTreeMap<String, usize>,
    reserved: &BTreeSet<String>,
) -> Option<&'a str> {
    let size = candidates.len();
    if size == 0 {
        return None;
    }
    let hash = Sha256::digest(casefold(strip(name)).as_bytes());
    let modulo = size as u128;
    let start = hash
        .iter()
        .fold(0, |n, byte| (n * 256 + u128::from(*byte)) % modulo);
    let start = usize::try_from(start).ok()?;
    let order = candidates[start..].iter().chain(&candidates[..start]);
    let unreserved = order.clone().any(|voice| !reserved.contains(voice));
    order
        .filter(|voice| !unreserved || !reserved.contains(*voice))
        .min_by_key(|voice| taken.get(*voice).copied().unwrap_or_default())
        .map(String::as_str)
}
