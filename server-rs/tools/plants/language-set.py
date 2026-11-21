#!/usr/bin/env python3
"""Wrong rules for the R152 engine order, admission and public answers."""
import runner

RUNS = {
    'vectors': ['test', '--test', 'language_set_vectors'],
    'host': ['test', '--test', 'host', 'a_language_set_is_heard_only_by_engines_the_owner_chose'],
}
KERNEL = 'clarp-kernel/src/heard/language_set.rs'
ORDER = 'clarp-kernel/src/heard.rs'
HOST = 'clarp-host/src/heard.rs'
PLANTS = []


def rules(file, run, changes):
    PLANTS.extend((file, run, name, old, new) for name, old, new in changes)


# The earlier plant for multiple models sending audio now lives in provider-once.py.
rules(KERNEL, 'vectors', [
    ('Local default aliases each get the audio', '        Engine::Own\n', '        Engine::Other(name)\n'),
    ('An explicit own model id is a different engine', 'engine_order::is_own(name, own_id)', 'engine_order::is_own(name, own_id.filter(|_| false))'),
    ('Norwegian Bokmal is treated as another language', '"nb" | "nn" => "no".to_string(),', '"nn" => "no".to_string(),'),
    ('Norwegian Nynorsk is treated as another language', '"nb" | "nn" => "no".to_string(),', '"nb" => "no".to_string(),'),
    ('An underscore stops a regional tag matching', "code.split(['-', '_'])", "code.split('-')"),
    ('A hyphen stops a regional tag matching', "code.split(['-', '_'])", "code.split('_')"),
    ('The off own engine is permitted', '''Own::Off => return Some("the Host's own engine is off".into()),''', 'Own::Off => return None,'),
    ('An unavailable engine is permitted', 'Own::Unavailable => return Some(OWN_UNAVAILABLE.into()),', 'Own::Unavailable => return None,'),
    ('The unavailable engine has different words', "the Host's own engine is unavailable", "the Host's own engine returned no words"),
    ('A failed own attempt has different words', "the Host's own engine could not hear it", "the Host's own engine failed"),
    ('The declaration keeps surrounding blanks', 'map(|code| strip(code).to_lowercase())', 'map(|code| code.to_lowercase())'),
    ('The declaration is case sensitive', 'map(|code| strip(code).to_lowercase())', 'map(|code| strip(code).to_string())'),
    ('Blank declarations are taken as languages', '        .filter(|code| !code.is_empty())\n', ''),
    ('An undeclared English-only model can take any set', 'if id.ends_with(".en")', 'if false'),
    ('An undeclared multilingual model only hears English', 'if id.ends_with(".en")', 'if true'),
    ('Multilingual is treated as a literal language code', 'if declared.iter().any(|code| code == "multilingual")', 'if false'),
    ('Any matching language is enough', 'languages.iter().any(|code| !heard.contains(&primary(code)))', 'languages.iter().all(|code| !heard.contains(&primary(code)))'),
    ('An unknown selected model is admitted', 'let (provider, _) = cloud_model(model)?;', 'let (provider, _) = cloud_model(model).or_else(|| cloud_model("google:chirp_3"))?;'),
    ('A selected provider that cannot take sets is admitted', 'SET_PROVIDERS.contains(&provider.id) && keyed(provider.id)', 'keyed(provider.id)'),
    ('An unkeyed selected model is admitted', 'SET_PROVIDERS.contains(&provider.id) && keyed(provider.id)', 'SET_PROVIDERS.contains(&provider.id)'),
    ('The rejecting provider is asked again', 'next == rejected || attempted.contains(&next)', 'attempted.contains(&next)'),
    ('The failed hosted provider is asked again', 'next == rejected || attempted.contains(&next)', 'next == rejected'),
    ('A failed local engine is asked again', '.contains(&Engine::Own)', '.contains(&Engine::Other("local"))'),
    ('The failed selected engine reason is hidden', 'reasons.push(format!("{target} already failed for this recording"));', 'reasons.push("engine failed".into());'),
    ('A refusal drops the own reason', 'reasons.push(why);', 'let _ = why;'),
    ('Incompatible providers are advertised in the error', '.filter(|provider| SET_PROVIDERS.contains(&provider.id))', '.filter(|_| true)'),
    ('Reasons are listed in the opposite order', 'reasons.join("; ")', 'reasons.iter().rev().cloned().collect::<Vec<_>>().join("; ")'),
    ('The refusal loses the chosen engine', '"engine": engine,', '"engine": null,'),
    ('The refusal loses the requested languages', '"languages": languages,\n        "engines": engines,', '"languages": [],\n        "engines": engines,'),
    ('The receipt always says detected', 'Some(codes) => json!(codes.split(\',\').collect::<Vec<_>>()),', 'Some(_) => json!("detected"),'),
    ('A detecting engine reports the requested set', 'None | Some("auto") => json!("detected"),', 'None | Some("auto") => json!(["no", "en"]),'),
    ('The receipt names the reverse route', '"from": from, "to": to', '"from": to, "to": from'),
    ('The receipt gives another reason', '"reason": "language_set", "languages": languages', '"reason": "outage", "languages": languages'),
])
rules(ORDER, 'vectors', [
    ('The route does not suppress repeated engine names', 'engine_order::distinct_engines(order, own_id)', '(order.collect(), Vec::new())'),
])
rules(HOST, 'host', [
    ('An unsupported hosted set is treated as an ordinary failure', '.map_err(|_| AttemptError::LanguageSet)?', '.map_err(|why| refuse(StatusCode::INTERNAL_SERVER_ERROR, why))?'),
    ('An earlier incompatible engine overrides the planned chain', 'AttemptError::LanguageSet if !more =>', 'AttemptError::LanguageSet =>'),
    ('The selected engine is taken from the original request', 'selected: &plan.selected,', 'selected: model,'),
    ('The selected hosted provider is told only English', 'chat_attempt(app, ears, target, language, listen_for, clip, false).await;\n            let heard', 'chat_attempt(app, ears, target, "en", listen_for, clip, false).await;\n            let heard'),
    ('The fallback records requested languages even for Scribe', 'language_set::receipt(plan.from, target, handed.as_deref())', 'language_set::receipt(plan.from, target, Some(language))'),
    ('The fallback receipt is never sent to the client', 'answer["engine_fallback"] = fallback;', 'let _ = fallback;'),
    ('The refusal is a 500', 'StatusCode::UNPROCESSABLE_ENTITY,\n            language_set::refusal', 'StatusCode::INTERNAL_SERVER_ERROR,\n            language_set::refusal'),
    ('The own engine raw failure reaches the client', 'reasons.push(language_set::OWN_FAILED.into());', 'reasons.push(body["error"].as_str().unwrap_or_default().into());'),
])

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
