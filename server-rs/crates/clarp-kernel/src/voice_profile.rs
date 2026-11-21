//! What an agent sounds like and how it is heard, as the Host resolves them (the Python Host's
//! `voice_profile.py`, its PR 485, contract 109).
//!
//! One resolver gives the voice a provider is handed for an agent's clip, and where it came
//! from: the agent's own (its entry in the owner's file, what it has stored, its persona's in
//! the owner's lists) or a provider's one default, which every agent without its own shares.
//! The speaker asks it for every provider it may try ([`speaking_order`]); the profile an app is
//! shown ([`speech`]) is made from the same attempts, so the two cannot say different things.
//! How an agent is heard is the plan of the engines a recording for it is tried with, and the
//! languages the first one is handed ([`hearing`]).
//!
//! [`HostView`] is all that is the same for every agent: the owner's settings of speech, which
//! keys there are, the names of voices in memory, the transcription settings. A snapshot makes
//! one and resolves every agent from it; [`profiles`] counts who shares a voice in one pass.
//! `origin` and `silent_reason` are open sets: a client shows plain words for one it does not
//! know.

use std::collections::BTreeMap;

use serde_json::{Map, Value, json};

use crate::heard;
use crate::languages;
use crate::said::{self, CARTESIA, ELEVENLABS, GEMINI, Route, Voices, Who};
use crate::voices;

pub const GOOGLE: &str = "google";
pub const DEEPGRAM: &str = "deepgram";
/// The agent's own voice, or its own engines or language.
pub const OWN: &str = "own";
/// A provider's one fallback voice, or the Host's engines or language.
pub const DEFAULT: &str = "default";
pub const NO_VOICE: &str = "no_voice";
pub const PROVIDER_OFF: &str = "provider_off";
pub const NOT_CONFIGURED: &str = "not_configured";
/// An outage: the provider did not make the clip.
pub const PROVIDER_FAILED: &str = "provider_failed";
/// The Host itself failed while making the clip.
pub const HOST_FAILED: &str = "host_failed";
/// The providers of speech built into the Host.
pub const BUILT: [&str; 5] = [CARTESIA, ELEVENLABS, GEMINI, DEEPGRAM, GOOGLE];

/// The voice a provider is handed for one agent, and where it came from.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Choice {
    pub voice: Option<String>,
    /// [`OWN`], [`DEFAULT`], or nothing when there is no voice.
    pub origin: Option<&'static str>,
}

impl Choice {
    fn own(voice: Option<String>) -> Self {
        let origin = voice.as_ref().map(|_| OWN);
        Self { voice, origin }
    }

    fn default(voice: Option<String>) -> Self {
        let origin = voice.as_ref().map(|_| DEFAULT);
        Self { voice, origin }
    }
}

/// Everything a profile needs that is the same for every agent: made once a snapshot.
#[derive(Debug, Clone, Default)]
pub struct HostView {
    /// The owner's settings of speech: the provider, the one tried after it, the entries of
    /// single agents, the persona lists, Gemini's voice for all.
    pub voices: Voices,
    /// Deepgram's voice for every agent without its own (`[deepgram] model`). Empty: none.
    pub deepgram_model: String,
    /// The providers of speech the Host has a key for (Google: whose credentials it has).
    pub keys: Vec<String>,
    /// Why Google's voices cannot be used at all, in the Python Host's code for it.
    pub google_refusal: Option<String>,
    /// The names of voices in memory, by provider and voice: never fetched for a profile.
    pub labels: BTreeMap<String, BTreeMap<String, String>>,
    /// The engine that stands in for the Host's own on a clip nobody named one for. Empty:
    /// none does.
    pub cloud_engine: String,
    /// The Host's own engine (`faster-whisper:small.en`). Empty: it has none.
    pub own_engine: String,
    /// The Host's language, as kept (one code or a set).
    pub language: String,
    /// The engines and the languages of single agents, as kept.
    pub agent_engines: Vec<(String, String)>,
    pub agent_languages: Vec<(String, String)>,
}

/// An agent as its profile needs it.
#[derive(Debug, Clone, Copy)]
pub struct Agent<'a> {
    pub who: Who<'a>,
    /// Its voice as stored.
    pub stored: Option<&'a str>,
}

/// What one agent's next clip is spoken with, or why nothing is heard.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Speech {
    pub provider: String,
    pub provider_label: String,
    pub voice_id: Option<String>,
    pub voice_label: Option<String>,
    pub origin: Option<&'static str>,
    pub streaming: bool,
    pub silent_reason: Option<&'static str>,
}

impl Speech {
    /// As an app is told it.
    pub fn told(&self) -> Map<String, Value> {
        let mut told = Map::new();
        told.insert("provider".into(), json!(self.provider));
        told.insert("provider_label".into(), json!(self.provider_label));
        told.insert("voice_id".into(), json!(self.voice_id));
        told.insert("voice_label".into(), json!(self.voice_label));
        told.insert("origin".into(), json!(self.origin));
        told.insert("streaming".into(), json!(self.streaming));
        told.insert("silent_reason".into(), json!(self.silent_reason));
        told
    }
}

/// A provider as the Host's catalogue names it, and whether its first audio comes before
/// the whole clip is made. One it does not list is called by its id.
pub fn provider_row(provider: &str) -> (String, bool) {
    match voices::provider_row(provider) {
        Some((name, streaming)) => (name, streaming),
        None => (provider.to_string(), false),
    }
}

/// The voice `provider` is handed for this agent's clip. The speaker asks it for every
/// provider it tries; the profile asks it too.
pub fn provider_voice(view: &HostView, agent: Agent<'_>, route: &Route, provider: &str) -> Choice {
    let persona = agent.who.persona;
    let stored = agent.stored;
    let saved = said::voice_for(stored, route, provider);
    let voices = &view.voices;
    match provider {
        CARTESIA => {
            Choice::own(saved.or_else(|| voices.cartesia_voice_for(persona).map(str::to_string)))
        }
        // A stored value without an ElevenLabs entry is handed over as it is.
        ELEVENLABS => {
            let voice = saved.or_else(|| stored.map(str::to_string));
            let origin = voice
                .as_deref()
                .filter(|voice| !voice.is_empty())
                .map(|_| OWN);
            Choice { voice, origin }
        }
        GEMINI => {
            let cartesia = provider_voice(view, agent, route, CARTESIA).voice;
            let own = saved
                .or_else(|| voices.gemini_voice_for(persona).map(str::to_string))
                .or_else(|| {
                    let cartesia = cartesia.as_deref().unwrap_or("");
                    voices
                        .gemini_voice_for_cartesia(cartesia)
                        .map(str::to_string)
                });
            if own.is_some() {
                return Choice::own(own);
            }
            let entry = voices.entry_for(agent.who);
            let moved = entry
                .iter()
                .rev()
                .find(|(key, _)| key == "provider")
                .is_some_and(|(_, provider)| provider == GEMINI);
            let default =
                (moved && !voices.gemini_voice.is_empty()).then(|| voices.gemini_voice.clone());
            Choice::default(default)
        }
        GOOGLE => {
            if saved.is_some() {
                return Choice::own(saved);
            }
            let bare = stored
                .map(str::trim)
                .filter(|voice| voices::google_has(voice));
            match bare {
                Some(voice) => Choice::own(Some(voice.to_string())),
                None => Choice::default(Some(voices::GOOGLE_DEFAULT_VOICE.to_string())),
            }
        }
        DEEPGRAM => {
            if saved.is_some() {
                return Choice::own(saved);
            }
            // An empty model is handed over as it is, and shown so (the reference's way).
            let model = view.deepgram_model.clone();
            let origin = (!model.is_empty()).then_some(DEFAULT);
            Choice {
                voice: Some(model),
                origin,
            }
        }
        // An engine this Host does not have: only a voice of the agent's own names one.
        _ => Choice::own(saved),
    }
}

/// Whether ElevenLabs can take `voice` as a voice id. A JSON text, a Cartesia uuid or an
/// empty value would be handed over and refused there.
pub fn elevenlabs_id_usable(voice: Option<&str>) -> bool {
    let value = voice.unwrap_or("").trim();
    let uuid = value.chars().count() == 36 && value.matches('-').count() == 4;
    !value.is_empty() && !value.starts_with('{') && !value.chars().any(char::is_whitespace) && !uuid
}

/// What a voice is called: a real name, or nothing (never an empty text).
fn voice_label(view: &HostView, provider: &str, voice: Option<&str>) -> Option<String> {
    let voice = voice.filter(|voice| !voice.is_empty())?;
    let label = if provider == GEMINI {
        (!voice.starts_with("voice_")).then(|| voice.to_string())
    } else {
        view.labels
            .get(provider)
            .and_then(|named| named.get(voice))
            .cloned()
    };
    label
        .map(|label| label.trim().to_string())
        .filter(|label| !label.is_empty())
}

/// What `provider` would say for this agent, or why it would be silent.
pub fn attempt(view: &HostView, agent: Agent<'_>, route: &Route, provider: &str) -> Speech {
    let choice = provider_voice(view, agent, route, provider);
    let keyed = view.keys.iter().any(|key| key == provider);
    let voiced = choice.voice.is_some();
    let silent = match provider {
        CARTESIA | GEMINI if !voiced => Some(NO_VOICE),
        CARTESIA | GEMINI | DEEPGRAM => (!keyed).then_some(NOT_CONFIGURED),
        ELEVENLABS if !keyed => Some(NOT_CONFIGURED),
        ELEVENLABS => (!elevenlabs_id_usable(choice.voice.as_deref())).then_some(NO_VOICE),
        GOOGLE if view.google_refusal.is_some() => Some(NOT_CONFIGURED),
        GOOGLE if !choice.voice.as_deref().is_some_and(voices::google_has) => Some(NO_VOICE),
        GOOGLE => (!keyed).then_some(NOT_CONFIGURED),
        _ => Some(NOT_CONFIGURED),
    };
    let usable = silent.is_none();
    let (provider_label, streaming) = provider_row(provider);
    Speech {
        provider: provider.to_string(),
        provider_label,
        voice_label: voice_label(view, provider, choice.voice.as_deref()).filter(|_| usable),
        voice_id: choice.voice.filter(|_| usable),
        origin: choice.origin.filter(|_| usable),
        streaming,
        silent_reason: silent,
    }
}

/// Speech is off for this agent: the Host's provider is none, or its entry turns it off.
fn off(view: &HostView, route: &Route) -> bool {
    view.voices.provider == "none" || route.provider == "none"
}

fn silent_off() -> Speech {
    let (provider_label, _) = provider_row("none");
    Speech {
        provider: "none".into(),
        provider_label,
        voice_id: None,
        voice_label: None,
        origin: None,
        streaming: false,
        silent_reason: Some(PROVIDER_OFF),
    }
}

/// The attempts the speaker makes, in its order: the provider of the agent's route, then the
/// one tried after it, each once.
fn attempts(view: &HostView, agent: Agent<'_>, route: &Route) -> Vec<Speech> {
    let mut made: Vec<Speech> = Vec::new();
    for provider in [route.provider.as_str(), route.fallback.as_str()] {
        if matches!(provider, "" | "none") || made.iter().any(|known| known.provider == provider) {
            continue;
        }
        made.push(attempt(view, agent, route, provider));
    }
    made
}

/// The provider and voice this agent's next clip is spoken with (the route's provider, else
/// the one tried after it when the first has no voice for it), or the reason nothing is heard.
pub fn speech(view: &HostView, agent: Agent<'_>) -> Speech {
    let route = view.voices.route(agent.who);
    if off(view, &route) {
        return silent_off();
    }
    let mut made = attempts(view, agent, &route);
    let primary = made.remove(0);
    if primary.silent_reason.is_some()
        && let Some(fallback) = made.into_iter().find(|tried| tried.silent_reason.is_none())
    {
        return fallback;
    }
    primary
}

/// Why nothing of an agent's clip can be said, and by which provider it would have been.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Unheard {
    pub reason: &'static str,
    pub provider: String,
}

/// What the speaker hands over, in the order it tries: each provider with the voice the
/// resolver gives it, those that can say it only. Nothing: why, as the profile says it. The
/// Host's speech being off is not told (`provider` `none`, reason [`PROVIDER_OFF`]).
pub fn speaking_order(view: &HostView, agent: Agent<'_>) -> Result<Vec<(String, String)>, Unheard> {
    let route = view.voices.route(agent.who);
    if off(view, &route) {
        return Err(Unheard {
            reason: PROVIDER_OFF,
            provider: "none".into(),
        });
    }
    let made = attempts(view, agent, &route);
    let by: Vec<(String, String)> = made
        .iter()
        .filter(|tried| tried.silent_reason.is_none())
        .filter_map(|tried| Some((tried.provider.clone(), tried.voice_id.clone()?)))
        .collect();
    if by.is_empty() {
        let primary = &made[0];
        return Err(Unheard {
            reason: primary.silent_reason.unwrap_or(NO_VOICE),
            provider: primary.provider.clone(),
        });
    }
    Ok(by)
}

/// What an engine is called on a screen: the contract's name for a cloud engine, the
/// catalogue's for a model of the Host's own, else its id.
pub fn engine_label(id: &str) -> String {
    if let Some((_, model)) = heard::cloud_model(id) {
        return model.name.to_string();
    }
    let listed = heard::inventory::CATALOG
        .iter()
        .find(|model| model.id() == id);
    listed.map_or_else(|| id.to_string(), |model| model.name().to_string())
}

fn engine_row(id: &str) -> Value {
    let (provider, model) = id.split_once(':').unwrap_or((id, ""));
    json!({ "id": id, "provider": provider, "model": model, "label": engine_label(id) })
}

fn codes(set: &str) -> Vec<String> {
    set.split(',')
        .filter(|code| !code.is_empty())
        .map(str::to_string)
        .collect()
}

/// The engines a recording for this agent is tried with, in order, and the languages the
/// first one is handed, as a clip the phone sends without a model of its own is planned.
pub fn hearing(view: &HostView, who: Who<'_>) -> Value {
    let own_engines = languages::of_agent(&view.agent_engines, who)
        .map(languages::engines)
        .unwrap_or_default();
    let plan = heard::plan("", &own_engines, &view.cloud_engine, "", false);
    let own_language = languages::of_agent(&view.agent_languages, who);
    let set = own_language.unwrap_or(&view.language);
    let engines: Vec<&str> = plan
        .chat_order(Some(&view.own_engine))
        .0
        .into_iter()
        .enumerate()
        .map(|(place, engine)| {
            if place == 0 && engine.is_empty() {
                view.own_engine.as_str()
            } else {
                engine
            }
        })
        .filter(|engine| !engine.is_empty())
        .collect();
    let engine_origin = if own_engines.is_empty() { DEFAULT } else { OWN };
    let mut language_origin = if own_language.is_some() { OWN } else { DEFAULT };
    let mut spoken = codes(set);
    let first = engines.first().copied().unwrap_or("");
    if !first.is_empty() && !heard::is_cloud(first) {
        // A local model's language is fixed when it loads: English for a .en model, found
        // by the model otherwise; the agent's is not used.
        spoken = if first.ends_with(".en") {
            vec!["en".into()]
        } else {
            Vec::new()
        };
        language_origin = DEFAULT;
    } else if heard::provider_of(first) == "elevenlabs" && spoken.len() > 1 {
        // Scribe is handed no language for a set: it finds it itself.
        spoken = Vec::new();
    }
    let device_may_override = engine_origin == DEFAULT;
    // What a hosted engine chosen on the phone is handed for this agent.
    let device_cloud = device_may_override.then(|| codes(set));
    json!({
        "engines": engines.iter().map(|engine| engine_row(engine)).collect::<Vec<_>>(),
        "engine_origin": engine_origin,
        "device_may_override": device_may_override,
        "device_cloud_languages": device_cloud,
        "languages": spoken,
        "language_origin": language_origin,
    })
}

/// One agent's profile, without who it shares its voice with.
pub fn profile(view: &HostView, agent: Agent<'_>) -> (Speech, Value) {
    (speech(view, agent), hearing(view, agent.who))
}

/// What a new agent gets before it exists. Its voice is known only once it is made (its
/// contact's), so speech names the provider alone.
pub fn defaults(view: &HostView) -> Value {
    let provider = view.voices.provider.as_str();
    let (provider_label, streaming) = provider_row(provider);
    let nobody = Who {
        name: "",
        persona: "",
        session: "",
    };
    json!({
        "speech": {
            "provider": provider, "provider_label": provider_label, "voice_id": null,
            "voice_label": null, "origin": DEFAULT, "streaming": streaming, "assignment": "contact",
            "silent_reason": if provider == "none" { Some(PROVIDER_OFF) } else { None },
        },
        "hearing": hearing(view, nobody),
    })
}

/// An agent of a snapshot as the sharing of voices needs it.
#[derive(Debug, Clone, Copy)]
pub struct Row<'a> {
    pub agent: Agent<'a>,
    /// Archived or a Janitor: it speaks to nobody and shares nobody's voice.
    pub inactive: bool,
}

/// Every row's profile as an app is told it, with `speech.shared_with`: how many OTHER
/// active agents speak with the same provider and voice (0: its own). One pass.
pub fn profiles(view: &HostView, rows: &[Row<'_>]) -> Vec<Value> {
    let made: Vec<(Speech, Value)> = rows.iter().map(|row| profile(view, row.agent)).collect();
    let key = |speech: &Speech| {
        speech
            .voice_id
            .clone()
            .map(|voice| (speech.provider.clone(), voice))
    };
    let mut users: BTreeMap<(String, String), usize> = BTreeMap::new();
    for ((speech, _), row) in made.iter().zip(rows) {
        if let (false, Some(key)) = (row.inactive, key(speech)) {
            *users.entry(key).or_default() += 1;
        }
    }
    made.into_iter()
        .zip(rows)
        .map(|((speech, hearing), row)| {
            let shared = key(&speech).map_or(0, |key| users.get(&key).copied().unwrap_or(0));
            let shared = if row.inactive {
                shared
            } else {
                shared.saturating_sub(1)
            };
            let mut told = speech.told();
            told.insert("shared_with".into(), json!(shared));
            json!({ "speech": told, "hearing": hearing })
        })
        .collect()
}

/// One agent's profile outside a snapshot (a creation answer, a roster event), its sharing
/// counted over `rows`, the Host's agents, when it is among them.
pub fn profile_among(view: &HostView, agent: Agent<'_>, rows: &[Row<'_>]) -> Value {
    let mut all: Vec<Row<'_>> = rows
        .iter()
        .filter(|row| row.agent.who.session != agent.who.session)
        .copied()
        .collect();
    let at = all.len();
    let inactive = rows
        .iter()
        .any(|row| row.agent.who.session == agent.who.session && row.inactive);
    all.push(Row { agent, inactive });
    profiles(view, &all).swap_remove(at)
}

/// The words of a clip that was not spoken; `partly` when an earlier clip of the same reply
/// was, so the reply is not shown as wholly silent.
pub fn not_spoken_words(reason: &str, provider_label: &str, name: &str, partly: bool) -> String {
    let provider = if provider_label.is_empty() {
        "The voice provider"
    } else {
        provider_label
    };
    let name = if name.is_empty() { "this agent" } else { name };
    let words = match reason {
        NO_VOICE => format!("Not spoken: {provider} has no voice for {name}."),
        NOT_CONFIGURED => format!("Not spoken: {provider} is not set up on this Host."),
        PROVIDER_OFF => "Not spoken: voice output is off on this Host.".to_string(),
        PROVIDER_FAILED => format!("Not spoken: {provider} did not make the clip."),
        HOST_FAILED => "Not spoken: the Host could not make the clip.".to_string(),
        _ => "Not spoken.".to_string(),
    };
    if !partly {
        return words;
    }
    let rest = words.strip_prefix("Not spoken: ").unwrap_or(&words);
    let rest = rest.strip_prefix("Not spoken.").unwrap_or(rest).trim();
    if rest.is_empty() {
        "Part of this reply was not spoken.".to_string()
    } else {
        format!("Part of this reply was not spoken: {rest}")
    }
}

/// The reason Google's own code for a failure stands for.
pub fn google_reason(code: &str) -> Option<&'static str> {
    match code {
        "google_credentials_missing"
        | "google_project_missing"
        | "google_auth_failed"
        | "google_id_collision"
        | "google_id_unverifiable" => Some(NOT_CONFIGURED),
        "voice_unavailable" => Some(NO_VOICE),
        _ => None,
    }
}

/// Google's own words for its codes: they name the operator's step.
pub fn google_words(code: &str) -> Option<&'static str> {
    Some(match code {
        "google_quota" => "Voice paused: Google Chirp 3 HD quota exceeded.",
        "google_auth_failed" => "Voice unavailable: Google Chirp 3 HD sign-in failed.",
        "google_credentials_missing" | "google_project_missing" => {
            "Voice unavailable: Google Chirp 3 HD is not configured."
        }
        "voice_unavailable" => "Voice unavailable: this Google Chirp 3 HD voice is not available.",
        "google_id_collision" => {
            "Voice unavailable: an old custom adapter also uses the id google; an operator must resolve it."
        }
        "google_id_unverifiable" => {
            "Voice unavailable: an installed custom adapter cannot be identified; an operator must check it."
        }
        _ => return None,
    })
}

/// Why a clip that ended without audio was not spoken, and by which provider: Google's code
/// decides for itself; else the profile's reason when it has one, else an outage of the
/// provider that was to speak.
pub fn clip_failure(view: &HostView, agent: Agent<'_>, google_code: Option<&str>) -> Unheard {
    if let Some(code) = google_code {
        return Unheard {
            reason: google_reason(code).unwrap_or(PROVIDER_FAILED),
            provider: GOOGLE.into(),
        };
    }
    let shown = speech(view, agent);
    Unheard {
        reason: shown.silent_reason.unwrap_or(PROVIDER_FAILED),
        provider: shown.provider,
    }
}

/// The names of voices the Host ships with: ElevenLabs', Deepgram's and Google's.
pub fn shipped_labels() -> BTreeMap<String, BTreeMap<String, String>> {
    voices::shipped_labels()
}

/// How long a turn is told once of a reason it was not spoken for.
pub const TURN_TOLD_MS: i64 = 6 * 3600 * 1000;
/// How long a clip of no turn is told once, by agent and reason.
pub const UNTRACED_TOLD_MS: i64 = 10 * 60 * 1000;
/// The most of each kind the Host remembers; the oldest go first.
pub const OUTCOMES_KEPT: usize = 1024;

/// What the apps were told of clips not spoken, and which turns had a clip spoken: so that a
/// turn is told once a reason, and a reply partly spoken is said to be so. Kept in memory
/// only, at most [`OUTCOMES_KEPT`] of each, pruned of what is older than its window at every
/// write; a restart forgets it.
#[derive(Debug, Default)]
pub struct Outcomes {
    told: std::collections::VecDeque<(String, String, &'static str, i64)>,
    spoke: std::collections::VecDeque<(String, String, i64)>,
}

fn window(trace: &str) -> i64 {
    if trace.is_empty() {
        UNTRACED_TOLD_MS
    } else {
        TURN_TOLD_MS
    }
}

impl Outcomes {
    fn prune(&mut self, now_ms: i64) {
        let old = |trace: &str, at: i64| now_ms - at > window(trace);
        self.told.retain(|(_, trace, _, at)| !old(trace, *at));
        self.spoke.retain(|(_, trace, at)| !old(trace, *at));
        while self.told.len() > OUTCOMES_KEPT {
            self.told.pop_front();
        }
        while self.spoke.len() > OUTCOMES_KEPT {
            self.spoke.pop_front();
        }
    }

    /// Whether the apps are to be told that a clip of this agent's turn was not spoken for
    /// this reason: the first time in the turn, and for a clip of no turn the first time in
    /// [`UNTRACED_TOLD_MS`]. Remembered when it is.
    pub fn tell(&mut self, agent: &str, trace: &str, reason: &'static str, now_ms: i64) -> bool {
        self.prune(now_ms);
        let known = self
            .told
            .iter()
            .any(|(a, t, r, _)| a == agent && t == trace && *r == reason);
        if !known {
            self.told
                .push_back((agent.to_string(), trace.to_string(), reason, now_ms));
            self.prune(now_ms);
        }
        !known
    }

    /// A clip of this agent's turn was spoken.
    pub fn spoke(&mut self, agent: &str, trace: &str, now_ms: i64) {
        self.spoke.retain(|(a, t, _)| !(a == agent && t == trace));
        self.spoke
            .push_back((agent.to_string(), trace.to_string(), now_ms));
        self.prune(now_ms);
    }

    /// Whether a clip of this agent's turn was spoken within its window.
    pub fn partly(&self, agent: &str, trace: &str, now_ms: i64) -> bool {
        let found = self.spoke.iter().find(|(a, t, _)| a == agent && t == trace);
        found.is_some_and(|(_, trace, at)| now_ms - at <= window(trace))
    }

    /// How many of each are remembered.
    pub fn kept(&self) -> (usize, usize) {
        (self.told.len(), self.spoke.len())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_turn_is_told_once_a_reason_and_what_is_remembered_is_bounded() {
        let mut outcomes = Outcomes::default();
        assert!(outcomes.tell("a", "t1", NO_VOICE, 0));
        assert!(
            !outcomes.tell("a", "t1", NO_VOICE, 1_000),
            "once a turn and reason"
        );
        assert!(
            outcomes.tell("a", "t1", PROVIDER_FAILED, 1_000),
            "another reason is told"
        );
        assert!(
            outcomes.tell("b", "t1", NO_VOICE, 1_000),
            "another agent is told"
        );
        assert!(
            outcomes.tell("a", "t2", NO_VOICE, 1_000),
            "another turn is told"
        );
        assert!(
            !outcomes.tell("a", "t1", NO_VOICE, TURN_TOLD_MS),
            "within the turn's window"
        );
        assert!(
            outcomes.tell("a", "t1", NO_VOICE, TURN_TOLD_MS + 1),
            "past it, told again"
        );
        // A clip of no turn: once in ten minutes.
        assert!(outcomes.tell("a", "", NO_VOICE, 0));
        assert!(!outcomes.tell("a", "", NO_VOICE, UNTRACED_TOLD_MS));
        assert!(outcomes.tell("a", "", NO_VOICE, UNTRACED_TOLD_MS + 1));
        // Pruned at every write: what is past its window is gone, and never more than the bound.
        let mut outcomes = Outcomes::default();
        outcomes.tell("a", "", NO_VOICE, 0);
        outcomes.tell("a", "t", NO_VOICE, 0);
        outcomes.tell("b", "t", NO_VOICE, UNTRACED_TOLD_MS + 1);
        assert_eq!(
            outcomes.kept().0,
            2,
            "the untraced one is pruned when the next is written"
        );
        for n in 0..(OUTCOMES_KEPT + 10) {
            let trace = format!("t{n}");
            outcomes.tell("a", &trace, NO_VOICE, 5);
            outcomes.spoke("a", &trace, 5);
        }
        assert_eq!(outcomes.kept(), (OUTCOMES_KEPT, OUTCOMES_KEPT));
        assert!(!outcomes.partly("a", "t0", 5), "the oldest went first");
        assert!(outcomes.partly("a", &format!("t{}", OUTCOMES_KEPT + 9), 5));
        assert!(!outcomes.partly(
            "a",
            &format!("t{}", OUTCOMES_KEPT + 9),
            5 + TURN_TOLD_MS + 1
        ));
    }
}
