#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

A turn's identity: the token every turn is given, which says whose turn it is and is signed with the Host's own
key (the kernel's rule of a token, `turn_identity.rs`), the key kept as one record and the reading of a token
against the agent's own log (`clarp-host/src/turn_identity.rs`, the actor's `standing_of`), the turn's launch
that hands it over (`agent.rs`) and the route that reads it (`library.rs`, `edge.rs`). Held by the vectors of the
Python Host's `caller_agent_id` (`turn_identity_vectors.rs`) and by the Host's test of turns that present their
tokens while they run, after they ended, after a restart and with a key that cannot be read.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'turn_identity_vectors'],
    'host': ['test', '--test', 'host', '--', 'a_turn_is_known_by_the_signed_token_it_is_given'],
}
K = 'clarp-kernel/src/turn_identity.rs'
H = 'clarp-host/src/turn_identity.rs'
C = 'clarp-actor/src/lib.rs'
A = 'clarp-host/src/agent.rs'
L = 'clarp-host/src/library.rs'
E = 'clarp-host/src/edge.rs'
# NOT PLANTED, each with its reason:
#   that a key made at the start which the store would not keep is used by nobody and said once in the Host's log
#     (`FleetHandle::unkept`): no Host test makes the store refuse a write before the fleet starts.
#   that a token with a field more names nobody: the MAC is over every sign before it, so a field more is a text
#     no key made, whatever the rule of fields says; only a token the Host made can verify.
#   that an agent that is gone names nobody: the Host needs the agent itself (its log) to read anything of a
#     token, so there is no agent to answer with once it is gone; the Host test holds it all the same.
#   that the key is made of the system's randomness and not of the fallback the Host's ids use: no test can
#     tell random bytes from bytes that only look so.
#   that a log that cannot be read is no answer (503) and not "nobody": no Host test makes an agent's log fail
#     under a running Host; a key that cannot be read, which takes the same way, is held.
SIGNED = '    signer.chain_update(said.as_bytes())\n'
OPENED = '            Ok(Some(kept)) => (key_of(&kept), None),\n'
MADE = 'made().filter(|key| store.put(&table, KEY, &record(key)).is_ok())'
PLANTS = [
    # The kernel's rule of a token.
    (K, 'host', 'a MAC is not checked', '    signed(key, said).verify_slice(&given).ok()?;', '    let _ = signed(key, said).verify_slice(&given);'),
    (K, 'host', 'the MAC is checked over fewer fields than it was made over', '    signed(key, said).verify_slice(&given).ok()?;', '    let fewer = said.rsplit_once(SEPARATOR).map_or(said, |(rest, _)| rest);\n    signed(key, fewer).verify_slice(&given).ok()?;'),
    (K, 'host', "a token of another agent is accepted (the MAC is not over the agent's id)", SIGNED, '    let mut fields = said.split(SEPARATOR);\n    let covered = format!("{}{}", fields.next().unwrap_or(""), fields.nth(1).unwrap_or(""));\n    signer.chain_update(covered.as_bytes())\n'),
    (K, 'vectors', 'a token of another version is read', '|| version != VERSION ||', '|| version.is_empty() ||'),
    (K, 'vectors', 'a MAC in capitals is read', "        b'a'..=b'f' => Some(sign - b'a' + 10),\n", "        b'a'..=b'f' => Some(sign - b'a' + 10),\n        b'A'..=b'F' => Some(sign - b'A' + 10),\n"),
    (K, 'vectors', 'a turn that ended eight days ago is accepted', '        Standing::Ended { at_ms } => now_ms <= at_ms.saturating_add(KEPT_MS),', '        Standing::Ended { .. } => true,'),
    (K, 'vectors', 'a turn names its agent a moment less than seven days after it ended', '        Standing::Ended { at_ms } => now_ms <= at_ms.saturating_add(KEPT_MS),', '        Standing::Ended { at_ms } => now_ms < at_ms.saturating_add(KEPT_MS),'),
    (K, 'host', 'a live turn is refused', '        Standing::Live => true,', '        Standing::Live => false,'),
    (K, 'host', 'a turn the log does not know names its agent', '        Standing::Unknown => false,', '        Standing::Unknown => true,'),
    (K, 'vectors', 'a token is kept for a day', 'pub const KEPT_MS: i64 = 7 * 86_400_000;', 'pub const KEPT_MS: i64 = 86_400_000;'),
    (K, 'host', 'the token is given under another name', 'pub const TURN_ENV: &str = "CLARP_PROVIDER_TURN";', 'pub const TURN_ENV: &str = "CLARP_TURN";'),
    # The key, and the reading of a token against the agent's log.
    (H, 'host', 'the key is made anew at every start', OPENED, f'            Ok(Some(_)) => ({MADE}, None),\n'),
    (H, 'host', 'a key that cannot be read is replaced by a new one', OPENED, f'            Ok(Some(kept)) => (key_of(&kept).or_else(|| {MADE}), None),\n'),
    (H, 'host', 'the key is shown', '    pub fn token(&self, agent_id: &str, turn: u64) -> Option<String> {\n        let key = self.key()?;\n', '    pub fn token(&self, agent_id: &str, turn: u64) -> Option<String> {\n        let key = self.key()?;\n        eprintln!("clarp-host: {}", key.iter().map(|b| format!("{b:02x}")).collect::<String>());\n'),
    (H, 'host', 'a token presented is written to a log line', '    let found = caller(app, token).await;\n', '    eprintln!("clarp-host: a turn presents {token}");\n    let found = caller(app, token).await;\n'),
    (H, 'host', "the turn's standing is not asked", '    let current = turn_identity::current(standing, app.clock.now_ms());', '    let _ = standing;\n    let current = turn_identity::current(turn_identity::Standing::Live, app.clock.now_ms());'),
    (H, 'host', 'a token that cannot be read is taken for nobody', '    match found.map_err(|why| ApiError::retryable(StatusCode::SERVICE_UNAVAILABLE, why.0))? {', '    match found.unwrap_or(None) {'),
    (H, 'host', 'a token that names nobody is taken for no token', '''        None => {
            let why = "verified turn identity required";
            Err(ApiError::new(StatusCode::FORBIDDEN, why))
        }''', '''        None => Ok(None),'''),
    (C, 'host', 'the end of another turn is read', '        Ok(match self.turn_end(turn)? {', '        Ok(match self.turn_end(turn + 1)? {'),
    (C, 'host', 'any turn of the agent runs while one does', '        if live == Some(turn) {', '        if live.is_some() {'),
    # The launch hands the token over.
    (A, 'host', 'a turn is given no token', '                    order.env.push((named, token));', '                    let _ = (named, token);'),
    (A, 'host', 'a turn is given the token of another turn', '                if let Some(token) = self.setup.turns.token(agent_id, order.turn.0) {', '                if let Some(token) = self.setup.turns.token(agent_id, order.turn.0 + 1) {'),
    # The route.
    (L, 'host', 'a caller identified without on_behalf_session is let through', '            if caller.is_some() && on_behalf_session.is_none() {', '            if caller.is_some() && on_behalf_session.is_none() && caller.is_none() {'),
    (E, 'host', 'the 503 does not say it may be asked again', '            let body = json!({ "error": self.error, "retryable": true });', '            let body = json!({ "error": self.error });'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
