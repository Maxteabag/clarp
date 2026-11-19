#!/usr/bin/env python3
"""Wrong rules for native session names, files, request admission and recovered bindings."""
import runner

PAST = 'clarp-backend/src/past.rs'
NAME = 'clarp-tool-args/src/lib.rs'
DOOR = 'clarp-host/src/edge.rs'
FORK = 'clarp-host/src/past.rs'
AGENT = 'clarp-kernel/src/agent.rs'
RUNS = {
    'lookup': ['test', '--lib', 'past::confinement::'],
    'door': ['test', '--test', 'host', 'session_ids_are_checked_before_creation_and_a_restored_agent_runs_fresh'],
}
PLANTS = []


def plant(file, group, why, old, new):
    PLANTS.append((file, group, why, old, new))


for why, old, new in [
    ('reject length 128', 'bytes.len() <= 128', 'bytes.len() < 128'),
    ('accept length 129', 'bytes.len() <= 128', 'bytes.len() <= 129'),
    ('accept empty', 'bytes.first().is_some_and(u8::is_ascii_alphanumeric)', 'bytes.first().is_none_or(u8::is_ascii_alphanumeric)'),
    ('allow initial punctuation', 'bytes.first().is_some_and(u8::is_ascii_alphanumeric)', '!bytes.is_empty()'),
    ('initial digits refused', 'u8::is_ascii_alphanumeric', 'u8::is_ascii_alphabetic'),
    ('initial letters refused', 'u8::is_ascii_alphanumeric', 'u8::is_ascii_digit'),
    ('a later digit refused', 'b.is_ascii_alphanumeric()', 'b.is_ascii_alphabetic()'),
    ('a later letter refused', 'b.is_ascii_alphanumeric()', 'b.is_ascii_digit()'),
    ('any character allowed after the first', 'b.is_ascii_alphanumeric()', 'true'),
    ('an underscore refused', "b'.' | b'_' | b'-'", "b'.' | b'-'"),
    ('a dash refused', "b'.' | b'_' | b'-'", "b'.' | b'_'"),
    ('a dot refused', "b'.' | b'_' | b'-'", "b'_' | b'-'"),
]:
    plant(NAME, 'lookup', why, old, new)

pattern = r'^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)\.jsonl\z'
for why, old, new in [
    ('allow text before rollout', '^rollout', 'rollout'),
    ('a non-rollout name taken', 'rollout-', '(?:rollout-)?'),
    ('short year allowed', r'\d{4}', r'\d{3,4}'),
    ('short month allowed', r'\d{4}-\d{2}', r'\d{4}-\d{1,2}'),
    ('short day allowed', r'\d{2}T', r'\d{1,2}T'),
    ('any date and time separator allowed', 'T', '.'),
    ('short hour allowed', r'T\d{2}', r'T\d{1,2}'),
    ('short minute allowed', r'T\d{2}-\d{2}', r'T\d{2}-\d{1,2}'),
    ('short second allowed', r'\d{2}-(.+)', r'\d{1,2}-(.+)'),
    ('any separator before the month allowed', r'\d{4}-', r'\d{4}.'),
    ('any separator before the second allowed', r'\d{2}-\d{2}-(.+)', r'\d{2}.\d{2}-(.+)'),
    ('non-digits allowed in the day', r'\d{2}T', '.{2}T'),
    ('reject Unicode decimal digits allowed by the reference', r'\d{4}', '[0-9]{4}'),
    ('the extension may start with any sign', r'\.jsonl', '.jsonl'),
    ('admit another suffix after jsonl', r'\z', ''),
]:
    plant(PAST, 'lookup', why, pattern, pattern.replace(old, new))
plant(PAST, 'lookup', 'take a partial id at the end of a longer one', '&matched[1] == session', 'matched[1].ends_with(session)')
plant(PAST, 'lookup', 'Muse rejects eight characters', '8..=64', '9..=64')
plant(PAST, 'lookup', 'Muse accepts seven characters', '8..=64', '7..=64')
plant(PAST, 'lookup', 'Muse rejects 64 characters', '8..=64', '8..=63')
plant(PAST, 'lookup', 'Muse accepts 65 characters', '8..=64', '8..=65')
plant(PAST, 'lookup', 'Muse allows non-hex letters', 'b.is_ascii_hexdigit()', 'b.is_ascii_alphanumeric()')
plant(PAST, 'lookup', 'Muse refuses a dash in a name', "b.is_ascii_hexdigit() || b == b'-'", "b.is_ascii_hexdigit()")
plant(PAST, 'lookup', 'Grok alone gets the Muse rule', 'if tool == Tool::Muse\n        &&', 'if tool == Tool::Grok\n        &&')
plant(PAST, 'lookup', 'a resolved file outside the store is taken', 'path.starts_with(base) && path.is_file()', 'path.is_file()')
plant(PAST, 'lookup', 'a sibling with the same name prefix counts as inside', 'path.starts_with(base)', 'path.to_string_lossy().starts_with(base.to_string_lossy().as_ref())')
plant(PAST, 'lookup', 'the suffix of a name suffices for Codex admission', 'if !one_name(session) || !sessions.is_dir()', 'if !sessions.is_dir()')
plant(PAST, 'lookup', 'Claude reads a rejected id', 'if !one_name(session) {\n        return Err(OutsideStore);', 'if false {\n        return Err(OutsideStore);')
plant(PAST, 'lookup', 'Grok reads a rejected id', 'if !one_name(id) {', 'if false {')
plant(PAST, 'lookup', 'Antigravity reads a rejected id', 'if !one_name(conversation) {\n        return None;', 'if false {\n        return None;')
plant('clarp-backend/src/history/opencode.rs', 'lookup', 'the shared database accepts a rejected key', 'if !clarp_kernel::native_session_name(id)', 'if false')
for why, old, new in [
    ('validate only resume', '["resume_session_id", "fork_session_id"]', '["resume_session_id"]'),
    ('validate only fork', '["resume_session_id", "fork_session_id"]', '["fork_session_id"]'),
    ('refuse the omitted empty string', 'id.is_empty()', 'false'),
    ('refuse surrounding space on a valid id', 'clarp_kernel::python::strip(id)', 'id'),
    ('admit a whitespace-only name', 'id.is_empty()', 'id.trim().is_empty()'),
    ('say something else on refusal', 'bad("invalid session id")', 'bad("bad session")'),
]:
    plant(DOOR, 'door', why, old, new)
plant(AGENT, 'door', 'retain the refused binding when replaying old events', '.filter(|id| crate::native_session_name(id))', '.filter(|_| true)')
plant(FORK, 'door', 'give the wrong answer for a forbidden copy', 'let why = "cannot fork that session";', 'let why = "fork io failed";')
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
