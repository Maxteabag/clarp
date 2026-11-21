#!/usr/bin/env python3
"""Wrong rules for the model, language and Cartesia synthesis boundary (R168)."""
import runner

RUNS = {
    'vectors': ['test', '--test', 'cartesia_language_vectors'],
    'host': ['test', '--test', 'host', 'every_agent_cartesia_request_uses_its_first_supported_language'],
    'cache': ['test', '--test', 'host', 'cached_router_phrases_follow_the_effective_cartesia_language'],
}

KERNEL = 'clarp-kernel/src/cartesia_language.rs'
VOICE = 'clarp-host/src/voice.rs'
WIRE = 'clarp-speech/src/cartesia.rs'
PLANTS = []


def rules(file, run, changes):
    PLANTS.extend((file, run, name, old, new) for name, old, new in changes)


rules(KERNEL, 'vectors', [
    ('Model names retain leading spaces', 'let model = strip(model).to_lowercase();', 'let model = model.to_lowercase();'),
    ('Model names are case-sensitive', 'let model = strip(model).to_lowercase();', 'let model = strip(model);'),
    ('The latest alias has only the older codes', 'model == "sonic-latest" || model.starts_with("sonic-3.6")', 'model.starts_with("sonic-3.6")'),
    ('A name beginning with latest is also the alias', 'model == "sonic-latest"', 'model.starts_with("sonic-latest")'),
    ('Dated 3.6 snapshots lose the new languages', 'model.starts_with("sonic-3.6")', 'model == "sonic-3.6"'),
    ('Odia is missing from 3.6', '(SONIC_3, &["or", "ur"])', '(SONIC_3, &["ur"])'),
    ('Urdu is missing from 3.6', '(SONIC_3, &["or", "ur"])', '(SONIC_3, &["or"])'),
    ('Sonic 3 speaks only the older Germanic set', '(SONIC_3, &[])', '(SONIC_2, &[])'),
    ('Sonic 3 snapshots are not included', 'model.starts_with("sonic-3")', 'model == "sonic-3"'),
    ('Norwegian is omitted from Sonic 3', '"hu", "no",', '"hu",'),
    ('The old plain sonic name is English only', 'model == "sonic" || model.starts_with("sonic-2024")', 'model.starts_with("sonic-2024")'),
    ('A dated 2024 snapshot is English only', 'model.starts_with("sonic-2024")', 'model == "sonic-2024"'),
    ('Sonic 2 also allows the newest languages', '(SONIC_2, &[])', '(SONIC_3, &[])'),
    ('Sonic turbo loses Hindi', '(SONIC_2, &["hi"])', '(SONIC_2, &[])'),
    ('An unknown model speaks Sonic 2 languages', '(&["en"], &[])', '(SONIC_2, &[])'),
    ('The first requested language bypasses the model', 'language == primary', 'language == primary || language == "en"'),
    ('A model fallback uses a different output code', '        code: "en",', '        code: "fr",'),
    ('The fallback omits the unsupported primary', '(!matches!(primary.as_str(), "" | "en")).then_some(primary)', 'None'),
    ('An empty request is logged as unsupported', '(!matches!(primary.as_str(), "" | "en")).then_some(primary)', 'Some(primary)'),
    ('An agent language is ignored', '.and_then(|who| of_agent(overrides, who))', '.and_then(|who| of_agent(overrides, who)).filter(|_| false)'),
    ('An unmatched agent always speaks English', '.unwrap_or(default)', '.unwrap_or("en")'),
    ('Take the last language from the set', "language.split(',').next()", "language.rsplit(',').next()"),
    ('A missing default leaves the language empty', '        "" => "en",', '        "" => "",'),
])
rules(VOICE, 'host', [
    ('An agent uses only the Host default', 'agent_speech_language(&overrides, Some(who), &default)', 'agent_speech_language(&overrides, None, &default)'),
    ('The startup default replaces selected settings', 'directory.setting(languages::LANGUAGE_KEY), &self.language', 'None, &self.language'),
    ('The adapter is given English for every request', 'Cartesia::new(owner::cartesia_model(file), language.code, read)', 'Cartesia::new(owner::cartesia_model(file), "en", read)'),
    ('The adapter forgets the unsupported language', '.with_language_fallback(language.unsupported)', '.with_language_fallback(None)'),
    ('The text preview forgets its session', 'Audition::Text(session) => (None, session),', 'Audition::Text(_) => (None, ""),'),
    ('Narration cache ignores the language', 'format!("{prefix}{key}-{}.mp3", language.code)', 'format!("{prefix}{key}.mp3")'),
])
rules(WIRE, 'host', [
    ('The serialized request always says English', '"language": self.language,', '"language": "en",'),
    ('The log loses the model that refused the code', 'cartesiaLanguageUnsupported {primary} on {model} -> en', 'cartesiaLanguageUnsupported {primary} -> en'),
])
rules(VOICE, 'cache', [
    ('The router phrase key loses the language', 'language: cartesia.as_ref().map(|language| language.code.to_string()),', 'language: None,'),
])

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
