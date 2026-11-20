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
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
