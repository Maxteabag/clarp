#!/usr/bin/env python3
"""Mutations of the alert router and its delivery at the Host."""
import runner

RUNS = {
    'vectors': ['test', '--test', 'push_direct_vectors'],
    'host': ['test', '--test', 'host', '--', 'an_alert_uses_the_hosts_key'],
}
K = 'clarp-kernel/src/alert_routes.rs'
H = 'clarp-host/src/pusher.rs'
PLANTS = [
    (K, 'vectors', 'direct mode still uses a phone grant', 'matches!(mode, "auto" | "relay")', 'matches!(mode, "auto" | "relay" | "direct")'),
    (K, 'vectors', 'a grant can be used without a relay', 'grants: relay && matches!', 'grants: matches!'),
    (K, 'vectors', 'relay mode falls back to a key', 'fallback: direct_allowed && key', 'fallback: key'),
    (K, 'vectors', 'a revoked grant is retained', 'grant_revoked = true;', 'grant_revoked = false;'),
    (K, 'vectors', 'another phone retries the failed credential relay', 'self.base = Some(Path::Direct);', 'self.base = Some(Path::Account);'),
    (K, 'vectors', 'a grant outage repeats for each phone', 'self.grant_down = Some(why.clone());', 'self.grant_down = None;'),
    (K, 'vectors', 'only a key or credential enables alerts', 'base.is_some() || (relay && relay_allowed)', 'base.is_some()'),
    (H, 'host', 'a permanent token rejection is temporary', 'if status == 410', 'if status == 411'),
]
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
