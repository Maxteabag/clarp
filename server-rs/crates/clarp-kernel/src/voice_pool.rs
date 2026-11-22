//! The deterministic voice assignment for a new agent or contact (V2b).

use std::collections::{BTreeMap, BTreeSet};
use std::sync::LazyLock;

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

/// An agent or contact that holds a voice.
pub struct User<'a> {
    pub name: &'a str,
    pub stored: &'a str,
    pub builtin: bool,
    pub agent_id: Option<&'a str>,
    pub created_at_ms: i64,
}

pub struct Exhausted {
    pub provider: &'static str,
    pub size: usize,
    pub shared_by: usize,
    pub reused: String,
    pub last_used_ms: i64,
}

#[derive(serde::Serialize)]
pub struct CatalogueMiss {
    pub language: String,
    pub total: usize,
}

pub struct Assignment {
    pub stored: String,
    pub exhausted: Vec<Exhausted>,
    pub catalogue_miss: Option<CatalogueMiss>,
}

const PROVIDERS: [&str; 3] = [CARTESIA, ELEVENLABS, GEMINI];

fn canonical(stored: &str) -> Map<String, Value> {
    said::voice_map(Some(&said::canonical_voice(stored)))
        .into_iter()
        .map(|(provider, id)| (provider, Value::String(id)))
        .collect()
}

/// The checked-in catalogue subset in the reference's order.
static CURATED: LazyLock<Vec<(String, String)>> = LazyLock::new(|| {
    let file: Value =
        serde_json::from_str(include_str!("cartesia_voice_pool.json")).unwrap_or_default();
    file["voices"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|row| {
            let id = row["id"].as_str().filter(|id| !id.is_empty())?;
            let name = row["name"].as_str().unwrap_or_default();
            Some((id.to_owned(), name.to_owned()))
        })
        .collect()
});

pub fn curated_names() -> BTreeMap<String, String> {
    CURATED
        .iter()
        .filter(|(_, name)| !name.is_empty())
        .cloned()
        .collect()
}

fn curated_voices(snapshot: &Snapshot, language: &str) -> Vec<String> {
    if languages::primary(language) != "en" {
        return Vec::new();
    }
    let catalogue = snapshot.catalogue.get(CARTESIA);
    let gone = snapshot.gone.get(CARTESIA);
    CURATED
        .iter()
        .filter(|(id, _)| {
            catalogue.is_none_or(|rows| rows.contains_key(id))
                && gone.is_none_or(|gone| !gone.contains(id))
        })
        .map(|(id, _)| id.clone())
        .collect()
}

fn without_placeholders(stored: &str) -> Map<String, Value> {
    let mut mapping = canonical(stored);
    mapping.retain(|_, id| !id.as_str().is_some_and(|id| id.starts_with("auto-")));
    mapping
}

/// Voices that can be assigned from today's Cartesia catalogue.
pub fn language_voices(
    snapshot: &Snapshot,
    language: &str,
) -> (Vec<String>, Option<CatalogueMiss>) {
    let Some(rows) = snapshot
        .catalogue
        .get(CARTESIA)
        .filter(|rows| !rows.is_empty())
    else {
        return (Vec::new(), None);
    };
    let language = languages::primary(language);
    let gone = snapshot.gone.get(CARTESIA);
    let ids: Vec<_> = rows
        .iter()
        .filter(|(id, row)| {
            row.public
                && !row.owner
                && !gone.is_some_and(|gone| gone.contains(*id))
                && languages::primary(&row.language) == language
        })
        .map(|(id, _)| id.clone())
        .collect();
    let miss = ids.is_empty().then_some(CatalogueMiss {
        language,
        total: rows.len(),
    });
    (ids, miss)
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

    fn candidates(&self, provider: &str, name: &str) -> (Vec<String>, Option<CatalogueMiss>) {
        if provider != CARTESIA {
            return (crate::voices::pool_ids(provider), None);
        }
        let mut seen = BTreeSet::new();
        let (source, miss) = if self.configured_cartesia.is_empty() {
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
            let curated = curated_voices(self.catalogue, language);
            if !curated.is_empty() {
                return (curated, None);
            }
            let (live, miss) = language_voices(self.catalogue, language);
            let source = if live.is_empty() {
                said::BUILT_IN_CARTESIA_VOICES
                    .iter()
                    .map(|(_, id)| (*id).into())
                    .collect()
            } else {
                live
            };
            (source, miss)
        } else {
            (self.configured_cartesia.to_vec(), None)
        };
        let ids = source
            .into_iter()
            .filter(|id| !id.is_empty() && seen.insert(id.clone()))
            .collect();
        (ids, miss)
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

    pub fn inherited(&self, name: &str, stored: &str, parent: &User<'_>) -> String {
        let mut mapping = without_placeholders(stored);
        let raw = sorted_ascii(&Value::Object(mapping.clone()));
        for provider in self.needed(name) {
            if self.speaks(&raw, name, provider).is_none()
                && let Some(voice) = self.speaks(parent.stored, parent.name, provider)
            {
                mapping.insert(provider.into(), Value::String(voice));
            }
        }
        sorted_ascii(&Value::Object(mapping))
    }

    pub fn assign(
        &self,
        name: &str,
        stored: &str,
        users: &[User<'_>],
        newest_clips: impl FnOnce() -> BTreeMap<String, i64>,
    ) -> Assignment {
        let mut mapping = without_placeholders(stored);
        let raw = sorted_ascii(&Value::Object(mapping.clone()));
        let missing = self
            .needed(name)
            .into_iter()
            .filter(|provider| self.speaks(&raw, name, provider).is_none());
        let newest_clips = std::cell::LazyCell::new(newest_clips);
        let mut exhausted = Vec::new();
        let mut catalogue_miss = None;
        for provider in missing {
            let (candidates, miss) = self.candidates(provider, name);
            catalogue_miss = catalogue_miss.or(miss);
            let mut reserved = self.reserves(provider);
            let mut taken = BTreeMap::<String, usize>::new();
            let mut holders = Vec::new();
            for user in users {
                if let Some(voice) = self.speaks(user.stored, user.name, provider) {
                    *taken.entry(voice.clone()).or_default() += 1;
                    if user.builtin {
                        reserved.insert(voice.clone());
                    }
                    holders.push((voice, user));
                }
            }
            let recency = || {
                let clips = &*newest_clips;
                let mut used = BTreeMap::<String, i64>::new();
                for (voice, user) in holders {
                    let at = user
                        .agent_id
                        .and_then(|id| clips.get(id).copied())
                        .filter(|at| *at != 0)
                        .unwrap_or(user.created_at_ms);
                    let last = used.entry(voice).or_default();
                    *last = (*last).max(at);
                }
                used
            };
            if let Some(picked) = pick(name, &candidates, &taken, &reserved, recency) {
                if let Some(last_used_ms) = picked.last_used_ms {
                    exhausted.push(Exhausted {
                        provider,
                        size: candidates.len(),
                        shared_by: taken.get(picked.id).copied().unwrap_or_default(),
                        reused: picked.id.to_owned(),
                        last_used_ms,
                    });
                }
                mapping.insert(provider.to_owned(), Value::String(picked.id.to_owned()));
            }
        }
        Assignment {
            stored: sorted_ascii(&Value::Object(mapping)),
            exhausted,
            catalogue_miss,
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

pub struct Pick<'a> {
    pub id: &'a str,
    /// Set when there was no free choice.
    pub last_used_ms: Option<i64>,
}

pub fn pick<'a>(
    name: &str,
    candidates: &'a [String],
    taken: &BTreeMap<String, usize>,
    reserved: &BTreeSet<String>,
    recency: impl FnOnce() -> BTreeMap<String, i64>,
) -> Option<Pick<'a>> {
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
    let order = order.filter(|voice| !unreserved || !reserved.contains(*voice));
    let count = |voice: &str| taken.get(voice).copied().unwrap_or_default();
    if let Some(id) = order.clone().find(|voice| count(voice) == 0) {
        return Some(Pick {
            id,
            last_used_ms: None,
        });
    }
    let used = recency();
    let at = |voice: &str| used.get(voice).copied().unwrap_or_default();
    let id = order.min_by_key(|voice| (at(voice), count(voice)))?;
    Some(Pick {
        id,
        last_used_ms: Some(at(id)),
    })
}
