#!/usr/bin/env python3
"""Wrong rules for the PR 498 profile projection, retained names and fetch budget."""
import runner

RUNS = {
    'rules': ['test', '--test', 'voice_names_vectors'],
    'profile': ['test', '--test', 'voice_profile_vectors'],
    'worker': ['test', '-p', 'clarp-host', '--lib', 'voice_names_refresh_uses_its_private_file'],
    'host': ['test', '--test', 'host', 'voice_names_and_configured_languages_are_projected'],
}
KERNEL = 'clarp-kernel/src/voice_names.rs'
PROFILE = 'clarp-kernel/src/voice_profile.rs'
HOST = 'clarp-host/src/profile.rs'
WORKER = 'clarp-host/src/voice/names.rs'
PLANTS = []


def rules(file, run, changes):
    PLANTS.extend((file, run, name, old, new) for name, old, new in changes)


rules(KERNEL, 'rules', [
    ('A new names file never refreshes at startup', 'age.is_none_or(|age| age >= REFRESH_EVERY)', 'age.is_some_and(|age| age >= REFRESH_EVERY)'),
    ('The exact day boundary is still fresh', 'age >= REFRESH_EVERY', 'age > REFRESH_EVERY'),
    ('An earlier age counts as old', 'age >= REFRESH_EVERY', 'age + Duration::from_secs(1) >= REFRESH_EVERY'),
    ('A short refresh budget drops a voice', 'pub const MAX_LOOKUPS: usize = 100;', 'pub const MAX_LOOKUPS: usize = 99;'),
    ('An extra voice is queried beyond the budget', 'pub const MAX_LOOKUPS: usize = 100;', 'pub const MAX_LOOKUPS: usize = 101;'),
    ('Listed voices spend the by-id budget again', '.filter(|id| !listed.contains_key(id.as_str()))', '.filter(|_| true)'),
    ('The catalogue is looked up in reverse order', '.take(MAX_LOOKUPS)', '.rev().take(MAX_LOOKUPS)'),
    ('A refreshed provider loses names that were not in its response', 'current.entry(provider).or_default().extend(names);', 'current.insert(provider, names);'),
    ('The previous names always win', 'current.entry(provider).or_default().extend(names);', 'current.entry(provider).or_insert(names);'),
])
rules(PROFILE, 'profile', [
    ('The configured language set uses the engine handoff', '"configured_languages": codes(set),', '"configured_languages": spoken,'),
    ('The configured language set is always the Host default', '"configured_languages": codes(set),', '"configured_languages": codes(&view.language),'),
    ('A silent agent keeps its origin', 'origin: choice.origin.filter(|_| usable),', 'origin: choice.origin,'),
])
rules(HOST, 'host', [
    ('The profile cannot see the names file', 'clarp_kernel::voice_names::merge(&mut labels, saved.names);', 'let _ = saved.names;'),
    ('Gemini labels carry no picker names', 'clarp_kernel::voices::gemini_labels(&view.voices),', 'Default::default(),'),
    ('Archived agents are still given a profile in roster events', 'member.record.session == session && member.record.archived_at_ms.is_none()', 'member.record.session == session'),
])
rules('clarp-kernel/src/voices.rs', 'host', [
    ('A Vertex variant is labelled as its ID', 'labels.insert(vertex.clone(), name);', 'labels.insert(vertex.clone(), vertex.clone());'),
])
rules(WORKER, 'worker', [
    ('A refresh enters while another is running', 'let Ok(_refresh) = self.refreshing.try_lock() else {\n            return Ok(());\n        };', 'let _refresh = self.refreshing.try_lock();'),
    ('The file never takes the checked time', 'File::open(&self.path)?.set_modified(SystemTime::now())?;', 'let _ = File::open(&self.path)?;'),
    ('A file containing equal names is rewritten', 'old_hash.is_some() && snapshot == before', 'false'),
    ('The file contents lack a digest', 'let hash = Some(digest(&raw));', 'let hash = None;'),
    ('The file hash changes to a constant after refresh', 'Some(digest(&raw))\n        };', 'Some(String::new())\n        };'),
    ('A successful refresh never publishes the new names', 'self.publish(Stored {\n            stamp: Some((meta.modified()?, meta.len())),\n            snapshot,\n            hash,\n        });', 'let _ = (meta, snapshot, hash);'),
    ('A failed catalogue discards the other providers too', 'Err(why) => eprintln!("voiceNamesFetchFail {CARTESIA}: {why}"),', 'Err(why) => { eprintln!("{why}"); return Snapshot::default(); }'),
    ('Cartesia skips the used IDs the catalogue missed', 'lookup(&missing, &mut listed, CARTESIA, |id| {', 'lookup(&BTreeSet::new(), &mut listed, CARTESIA, |id| {'),
    ('An ElevenLabs provider with used voices is skipped', '&& !eleven_ids.is_empty()', '&& eleven_ids.is_empty()'),
])
rules('clarp-speech/src/cartesia.rs', 'worker', [
    ('The complete background catalogue is filtered to English', 'catalogue(key, endpoint, false, 20)', 'catalogue(key, endpoint, true, 20)'),
    ('The catalogue stops at the first page', 'catalogue(key, endpoint, false, 20)', 'catalogue(key, endpoint, false, 1)'),
    ('A cursor is handed without being escaped', 'crate::voice_names::segment(&after)', 'after.clone()'),
])
rules('clarp-speech/src/eleven_voice.rs', 'worker', [
    ('The by-id ElevenLabs label includes its description', '.split(" - ")', '.split(" / ")'),
])

# The production worker's startup delay and daily sleep use the shared durations.
# The age predicate is tested at both sides and at the exact day boundary.
# No mutation is allowed to turn a missing endpoint or missing key into a real provider contact.

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
