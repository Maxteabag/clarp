#!/usr/bin/env python3
"""Wrong rules for keeping the router's own spoken words."""
import runner

HOST = 'clarp-host/src/router/phrases.rs'
FLOW = 'clarp-kernel/src/router/flow.rs'
RULES = 'clarp-kernel/src/router/phrases.rs'
VOICE = 'clarp-host/src/voice.rs'
RUNS = {
    'words':['test','--test','router_phrases','fixed_words'],
    'bounds':['test','--test','router_phrases','eviction_keeps'],
    'door':['test','--test','host','the_router_reuses_only_its_own_words'],
}
PLANTS = [
    (FLOW,'words','compare words after trimming','phrase.text == text','phrase.text == text.trim()'),
    (RULES,'words','omit the cached marker','"cached_phrase":true','"cached_phrase":false'),
    (RULES,'words','use the origin session','"session":session','"session":""'),
    (RULES,'words','omit the current agent','"agent_id":agent','"agent_id":null'),
    (RULES,'words','announce the engine voice in place of the app voice','"voice_id":voice','"voice_id":""'),
    (RULES,'bounds','evict at the count limit','count >= MAX_COUNT','count + 1 >= MAX_COUNT'),
    (RULES,'bounds','evict at the byte limit','bytes > MAX_BYTES','bytes >= MAX_BYTES'),
    (RULES,'bounds','one more clip fits','pub const MAX_COUNT: usize = 1024;','pub const MAX_COUNT: usize = 1025;'),
    (RULES,'bounds','one more byte fits a clip','pub const MAX_CLIP: usize = 256 * 1024;','pub const MAX_CLIP: usize = 256 * 1024 + 1;'),
    (RULES,'bounds','one more byte fits all audio','pub const MAX_BYTES: usize = 64 * 1024 * 1024;','pub const MAX_BYTES: usize = 64 * 1024 * 1024 + 1;'),
    (HOST,'door','a stale sentence plays','phrase_key(&row.text) == Some(row.key.phrase.as_str())','true'),
]
PLANTS.append((HOST,'door','discard the book at restore','if let Some(raw) = raw {','if let Some(raw) = raw.filter(|_| false) {'))
RUNS.update({
    'space':['test','--test','host','router_phrase_eviction_releases_only_unreferenced_audio'],
    'store':['test','--lib','a_failed_phrase_record_never_commits_audio_or_evicts_a_kept_clip'],
    'name':['test','--test','router_phrases','only_a_digest'],
})
PLANTS += [
    (RULES,'name','permit a truncated digest','digest.len() == 64','digest.len() >= 63'),
    (RULES,'name','permit uppercase hex',"(b'a'..=b'f').contains(&b)",'b.is_ascii_hexdigit()'),
    (RULES,'name','serve a different audio namespace','strip_prefix("phrase-")','strip_prefix("other-")'),
    (RULES,'name','serve another file type','strip_suffix(".mp3")','strip_suffix(".ogg")'),
    (RULES,'words','cached event has another type','"type":"audio"','"type":"phrase"'),
    (RULES,'words','cached event has no clip URL','format!("/audio/{name}")','format!("/{name}")'),
    (RULES,'words','cached event has no key','"phrase_key":phrase','"phrase_key":""'),
    (RULES,'bounds','let one byte over the total through','bytes > MAX_BYTES','bytes > MAX_BYTES + 1'),
    (HOST,'store','an uncommitted blob is left behind','forget(store, &file);','let _ = &file;'),
    (HOST,'store','remove audio another row still owns on failure','if !rows.iter().any(|r| r.name == file) {','if true {'),
    (HOST,'store','publish audio metadata even when the write failed','if !write(store, &next).await {','if !write(store, &next).await && false {'),
    (HOST,'store','missing store is treated as empty','Err(why) => {\n            store.fleet.unkept(KIND, &why.0);\n            return None;', 'Err(_) => {\n            None'),
    (HOST,'store','clean another file in the same shard','phrases::blob_name(&name) && !live.contains(name.as_str())','!live.contains(name.as_str())'),
    (HOST,'store','skip orphan collection','!live.contains(name.as_str())','false && !live.contains(name.as_str())'),
    (HOST,'space','forget the newest fill instead','next.drain(..phrases::excess(&next));','next.truncate(next.len() - phrases::excess(&next));'),
    (HOST,'space','remove shared blobs on eviction','!live.contains(row.name.as_str()) && !forget(store, &row.name)','!forget(store, &row.name)'),
    (HOST,'space','keep evicted blobs on disk','&& !forget(store, &row.name)','&& false'),
    (HOST,'space','skip the only copy of a fresh blob','if let Err(why) = store.blobs.put(&file, bytes)', 'if let Err(why) = store.blobs.put(&file, &[])'),
    (HOST,'space','hide an invalid stored row','store.fleet.unkept(KIND, "invalid phrase record");','let _ = KIND;'),
    (HOST,'door','trust a corrupted clip of the same length','name(&bytes) == row.name','true'),
    (HOST,'door','silently lose a damaged served clip','store.fleet.unkept(KIND, "unreadable phrase clip");\n        Self::drop_audio','Self::drop_audio'),
    (HOST,'door','cache solely by phrase regardless of voice','position(|row| &row.key == key)','position(|row| row.key.phrase == key.phrase)'),
    (HOST,'door','a hit speaks again','Some(name) => (Playback::Cached(name), None),','Some(_) => (Playback::Retry, None),'),
    (HOST,'door','retry the same synthesized phrase as fallback','return matches!(clip, Playback::Retry);','return true;'),
    (HOST,'door','a refused primary skips fallback','why.may_be_said_by_another()','false && why.may_be_said_by_another()'),
    (VOICE,'door','treat a fallback as the primary','(&route.fallback, false)','(&route.fallback, true)'),
    (VOICE,'door','cache across different models','said::CARTESIA => owner::cartesia_model(file),','said::CARTESIA => String::new(),'),
    (VOICE,'space','do not keep a clip exactly at its limit','sound.len() <= clarp_kernel::router::phrases::MAX_CLIP','sound.len() < clarp_kernel::router::phrases::MAX_CLIP'),
]

# Each table sentence has to address its own key even when the caller supplied another.
for phrase in ('CLARIFY_TARGET', 'AMBIGUOUS_TARGET', 'REPEAT_MISSING', 'CONTROL_UNKNOWN',
               'AGENT_CONTROL_UNKNOWN', 'NO_SUCH_AGENT', 'WHICH_AGENT', 'CANNOT_CHECK',
               'CANNOT_COMMAND', 'UNSURE_COMMAND', 'NO_VOICE_COMMANDS', 'FORK_IN_APP',
               'NAME_TAKEN', 'COMMAND_REFUSED', 'NO_VALID_TARGET', 'HELD_MESSAGE_GONE'):
    PLANTS.append((FLOW, 'words', f'{phrase.lower()} is absent from the table',
                   f'        {phrase},\n', ''))
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
