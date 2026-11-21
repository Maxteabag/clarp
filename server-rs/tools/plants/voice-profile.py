#!/usr/bin/env python3
"""The voice a provider gets, what the apps see, and how silence is told."""
import runner

RUNS = {
    'vectors': ['test', '--test', 'voice_profile_vectors'],
    'outcomes': ['test', '-p', 'clarp-kernel', '--lib', 'a_turn_is_told_once_a_reason'],
    'routing': ['test', '-p', 'clarp-host', '--lib', 'the_owner_chooses_who_speaks'],
    'host': ['test', '--test', 'host', 'an_agents_voice_and_hearing_are_shown'],
    'hearing-route': ['test', '--test', 'host', 'a_clip_is_heard_by_an_engine_over_the_network'],
    'native-fork': ['test', '--test', 'host', 'a_new_agent_begins_with_a_session_its_tool_kept'],
    'clip': ['test', '--test', 'host', 'what_an_agent_says_aloud_becomes_a_clip'],
    'contract': ['test', '--test', 'host', 'a_message_is_answered_and_the_apps_are_told'],
}

KERNEL = 'clarp-kernel/src/voice_profile.rs'
VOICE = 'clarp-host/src/voice.rs'
EDGE = 'clarp-host/src/edge.rs'
PROFILE = 'clarp-host/src/profile.rs'
PLANTS = []


def rules(file, run, changes):
    PLANTS.extend((file, run, name, old, new) for name, old, new in changes)


rules(KERNEL, 'vectors', [
    ('Cartesia skips the persona voice', 'voices.cartesia_voice_for(persona).map(str::to_string)', 'None'),
    ('ElevenLabs discards the legacy bare stored id', 'stored.map(str::to_string)', 'None'),
    ('Gemini ignores the persona voice', 'voices.gemini_voice_for(persona).map(str::to_string)', 'None'),
    ('Gemini ignores the Cartesia-linked voice', '.gemini_voice_for_cartesia(cartesia)', '.gemini_voice_for_cartesia("")'),
    ('Gemini ignores the explicit provider override', 'moved && !voices.gemini_voice.is_empty()', '!voices.gemini_voice.is_empty()'),
    ('Gemini reports its fallback voice as an own voice', 'Choice::default(default)', 'Choice::own(default)'),
    ('Google ignores a bare allowed id', '.filter(|voice| voices::google_has(voice))', '.filter(|_| false)'),
    ('Google treats the shared voice as an own voice', 'Choice::default(Some(voices::GOOGLE_DEFAULT_VOICE.to_string()))', 'Choice::own(Some(voices::GOOGLE_DEFAULT_VOICE.to_string()))'),
    ('Deepgram treats its shared model as an own voice', '(!model.is_empty()).then_some(DEFAULT)', '(!model.is_empty()).then_some(OWN)'),
    ('ElevenLabs accepts a JSON text', " && !value.starts_with('{')", ''),
    ('ElevenLabs accepts embedded whitespace', ' && !value.chars().any(char::is_whitespace)', ''),
    ('ElevenLabs accepts a Cartesia UUID', ' && !uuid', ''),
    ('ElevenLabs refuses ids with no dashes at the UUID length', "value.matches('-').count() == 4", "value.matches('-').count() <= 4"),
    ('ElevenLabs refuses short ids with four dashes', 'value.chars().count() == 36', 'value.chars().count() <= 36'),
    ('ElevenLabs treats an empty id as usable', '!value.is_empty() && !value.starts_with', '!value.starts_with'),
    ('Gemini shows an opaque unregistered id as a label',
     '.and_then(|named| named.get(voice))\n        .cloned();',
     '.and_then(|named| named.get(voice))\n        .cloned().or_else(|| (provider == GEMINI).then(|| voice.to_string()));'),
    ('A voice without a label is shown with a made-up name', '.map(|label| label.trim().to_string())', '.or_else(|| Some("Unnamed".into())).map(|label| label.trim().to_string())'),
    ('Cartesia and Gemini without a voice report a configuration failure', 'CARTESIA | GEMINI if !voiced => Some(NO_VOICE)', 'CARTESIA | GEMINI if !voiced => Some(NOT_CONFIGURED)'),
    ('Cartesia, Gemini and Deepgram speak without a key', 'CARTESIA | GEMINI | DEEPGRAM => (!keyed).then_some(NOT_CONFIGURED)', 'CARTESIA | GEMINI | DEEPGRAM => None'),
    ('ElevenLabs reports a missing key as no voice', 'ELEVENLABS if !keyed => Some(NOT_CONFIGURED)', 'ELEVENLABS if !keyed => Some(NO_VOICE)'),
    ('ElevenLabs has no silence gate for an unusable id', '(!elevenlabs_id_usable(choice.voice.as_deref())).then_some(NO_VOICE)', 'None'),
    ('Google disregards a credential refusal', 'GOOGLE if view.google_refusal.is_some() => Some(NOT_CONFIGURED),', ''),
    ('Google accepts an id off the allowlist', 'GOOGLE if !choice.voice.as_deref().is_some_and(voices::google_has) => {\n            Some(VOICE_UNAVAILABLE)\n        }', ''),
    ('Google uses a voice even without a key', 'GOOGLE => (!keyed).then_some(NOT_CONFIGURED)', 'GOOGLE => None'),
    ('The Host-wide off switch loses to an agent override', 'view.voices.provider == "none" || route.provider == "none"', 'route.provider == "none"'),
    ('A silent primary never falls back', 'if primary.silent_reason.is_some()', 'if false && primary.silent_reason.is_some()'),
    ('A configured fallback always wins over an audible primary', 'if primary.silent_reason.is_some()', 'if true'),
    ('An unavailable fallback replaces the primary refusal', '.find(|tried| tried.silent_reason.is_none())', '.next()'),
    ('The speaker reverses which available provider speaks first', '.iter()\n        .filter(|tried| tried.silent_reason.is_none())', '.iter()\n        .rev()\n        .filter(|tried| tried.silent_reason.is_none())'),
    ('The speaker hands over a different id than the profile', 'Some((tried.provider.clone(), tried.voice_id.clone()?))', 'Some((tried.provider.clone(), "other-voice".into()))'),
    ('The speaker loses the primary silent reason', 'reason: primary.silent_reason.unwrap_or(NO_VOICE)', 'reason: NO_VOICE'),
    ('A personal engine falls behind the Host engine', 'heard::plan("", &own_engines, &view.cloud_engine, "", false)', 'heard::plan("", &[], &view.cloud_engine, "", false)'),
    ('The Host own model is dropped when no model stands in', 'view.own_engine.as_str()', '""'),
])
rules('clarp-kernel/src/heard.rs', 'vectors', [
    ('The later hearing engines are omitted', '.chain(self.fallbacks.iter().map(String::as_str))', '.chain(std::iter::empty())'),
])
rules(KERNEL, 'vectors', [
    ('A personal engine can be overridden by the device', 'let device_may_override = engine_origin == DEFAULT;', 'let device_may_override = true;'),
    ('The device never learns the configured set', 'device_may_override.then(|| codes(set))', 'Some(Vec::<String>::new())'),
    ('Local English models autodetect the language', 'if first.ends_with(".en")', 'if false'),
    ('A local model uses the agent language origin', 'language_origin = DEFAULT;', 'language_origin = OWN;'),
    ('ElevenLabs is handed a language set', 'heard::provider_of(first) == "elevenlabs" && spoken.len() > 1', 'false'),
    ('A new agent is described without its contact assignment', '"assignment": "contact"', '"assignment": "voice"'),
    ('A new agent has no off indication', 'if provider == "none" { Some(PROVIDER_OFF) } else { None }', 'None::<&str>'),
    ('Archived agents and janitors count towards sharing', '(false, Some(key)) = (row.inactive, key(speech))', 'Some(key) = key(speech)'),
    ('An active agent counts its own voice as shared', 'shared.saturating_sub(1)', 'shared'),
    ('Speech without a voice loses the agent name', 'has no voice for {name}.', 'has no voice.'),
    ('Missing configuration is described as an outage', 'is not set up on this Host.', 'did not make the clip.'),
    ('The off switch is described as a Host fault', 'voice output is off on this Host.', 'the Host could not make the clip.'),
    ('A provider outage is described as a Host fault', '{provider} did not make the clip.', 'the Host could not make the clip.'),
    ('A Host fault is described as a provider outage', 'the Host could not make the clip.', 'the voice provider did not make the clip.'),
    ('Partial speech is described as complete silence', 'if !partly {', 'if true {'),
    ('The fallback label for an unnamed provider is blank', '"The voice provider"', '""'),
    ('The fallback name for an unnamed agent is blank', '"this agent"', '""'),
    ('Google maps a voice refusal to a configuration problem', '"voice_unavailable" => Some(VOICE_UNAVAILABLE)', '"voice_unavailable" => Some(NOT_CONFIGURED)'),
    ('Google quota sounds like a sign-in failure', 'Voice paused: Google Chirp 3 HD quota exceeded.', 'Voice unavailable: Google Chirp 3 HD sign-in failed.'),
])

rules(KERNEL, 'outcomes', [
    ('The exact end of the notice window expires too soon', 'now_ms - at > window(trace)', 'now_ms - at >= window(trace)'),
    ('The notice window lasts a millisecond too long', 'now_ms - at > window(trace)', 'now_ms - at > window(trace) + 1'),
    ('An untraced notice lives as long as a turn notice', 'UNTRACED_TOLD_MS\n    } else', 'TURN_TOLD_MS\n    } else'),
    ('The same reason is told for every clip of the turn', 'if !known {', 'if true {'),
    ('A second reason is suppressed', 'a == agent && t == trace && *r == reason', 'a == agent && t == trace'),
    ('Another agent does not get its own notice', 'a == agent && t == trace && *r == reason', 't == trace && *r == reason'),
    ('The next turn does not get its own notice', 'a == agent && t == trace && *r == reason', 'a == agent && *r == reason'),
    ('The notice immediately exceeds its cap', 'while self.told.len() > OUTCOMES_KEPT', 'while self.told.len() > OUTCOMES_KEPT + 1'),
    ('The oldest notice is kept in place of the newest', 'self.spoke.pop_front();', 'self.spoke.pop_back();'),
    ('A stale spoken clip makes a later reply partly spoken', 'now_ms - at <= window(trace)', 'now_ms - at <= window(trace) + 1'),
])

rules(EDGE, 'host', [
    ('The snapshot omits the agent profile', 'row["voice_profile"] = profile;', 'row["voice_profile"] = Value::Null;'),
    ('An archived agent is shown with a profile', 'if !archived.contains(session)', 'if !session.is_empty()'),
    ('The snapshot omits the defaults', '"voice_defaults": voice_defaults,', ''),
    ('The snapshot omits the version', '"voice_profile_version": voice_profile_version,', ''),
    ('The creation answer omits the profile', 'agent.insert("voice_profile".into(), profile);', 'let _ = profile;'),
])
rules(PROFILE, 'host', [
    ('The roster never includes a profile on creation', '    "created",', '    "no-created",'),
    ('The roster never includes a profile on relaunch', '    "relaunched",', '    "no-relaunched",'),
    ('The roster never includes a profile on contact assignment', '    "contact-assigned",', '    "no-contact-assigned",'),
    ('The roster never includes a profile on rename', '    "agent-renamed",', '    "no-agent-renamed",'),
    ('The roster never includes a profile on a voice change', '    "voice-changed",', '    "no-voice-changed",'),
    ('A change is announced under another kind', '"kind": "voice-profile"', '"kind": "voice-settings"'),
    ('The version never changes', 'let digest = Sha256::digest(format!("{view:?}").as_bytes());', 'let digest = Sha256::digest([]);'),
])
rules(PROFILE, 'native-fork', [
    ('The roster never includes a profile on a native fork', '    "forked",', '    "no-forked",'),
])
rules(VOICE, 'host', [
    ('The profile never recognizes an available key', 'OwnerFile::key_from(Some(owners), key_of(provider, owners)).is_some()', 'false'),
])
rules(VOICE, 'routing', [
    ('A Host with speech off reports a refusal', 'unheard.provider == "none" && view.voices.provider == "none"', 'false'),
    ('An agent with speech off and a live Host is never told', 'unheard.provider == "none" && view.voices.provider == "none"', 'unheard.provider == "none"'),
])
rules(VOICE, 'clip', [
    ('Every failed clip tells the turn again', '        if !first {', '        if false {'),
])
rules('clarp-host/src/heard.rs', 'hearing-route', [
    ('A hosted ElevenLabs model declines language sets', 'languages::SET_PROVIDERS.contains(&provider.id)', 'false'),
])
rules('clarp-host/src/wire.rs', 'clip', [
    ('The speech error reports only a generic reason', '"reason": reason,\n    });\n    if !provider.is_empty()', '"reason": "unknown",\n    });\n    if !provider.is_empty()'),
    ('The speech error loses its provider', 'told["provider"] = json!(provider);', 'told["provider"] = Value::Null;'),
    ('The speech error loses its turn trace', 'told["trace_id"] = json!(trace);', 'told["trace_id"] = Value::Null;'),
])

rules(EDGE, 'contract', [
    ('The voice profile is advertised at the previous contract', '("voice_profile", 109)', '("voice_profile", 108)'),
    ('Speech outcomes are advertised at the previous contract', '("speech_outcome", 109)', '("speech_outcome", 108)'),
])

# Gemini's surface translation belongs to its provider adapter: the profile and resolver
# describe the logical id, before this provider-specific translation, as on the Python Host.
# The key file's read count and the snapshot's latency are held by source structure and the
# disposable benchmark, not a timing assertion in a behavioral test.

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
